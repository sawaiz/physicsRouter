"""Step-based autorouting pipeline (inspired by tscircuit capacity-autorouter).

Stages:
  1. pin_access — legal pad→via sites
  2. topology — multipin Kruskal trees
  3. capacity_mesh — hierarchical capacity cells
  4. global_sections — layer + corridor negotiation on the mesh
  5. detailed — hybrid / clearance_aware copper
  6. manufacturing_gate — full connectivity + native DRC

API mirrors tscircuit's ``while not solver.solved: solver.step()`` pattern so
UI/jobs can show phase progress without embedding the TS package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from physics_router.capacity_mesh import CapacityMesh, build_capacity_mesh, path_through_mesh
from physics_router.design_rules import DesignRules, default_design_rules
from physics_router.global_router import GlobalRoutePlan, build_global_route_plan
from physics_router.graph_theory import plan_graph_topology
from physics_router.models import BoardModel, PlacementConfig
from physics_router.pin_access import PinAccessPlan, build_pin_access_plan
from physics_router.router import RouteResult, _net_fully_connected, native_drc_check


ProgressCallback = Callable[[int, int, str, str, dict], None]


@dataclass
class PipelineStageResult:
    name: str
    ok: bool
    elapsed_hint: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoutePipelineSolver:
    """Production autoroute pipeline with explicit stages and fail-loud gates."""

    board: BoardModel
    config: PlacementConfig | None = None
    rules: DesignRules | None = None
    effort: float = 0.55
    capacity_depth: int | None = None
    progress_cb: ProgressCallback | None = None
    # When True, pick via_0p6 vs via_0p45 by pin-access reachability before routing.
    auto_via_profile: bool = True
    via_profiles: tuple[str, ...] = ("via_0p6", "via_0p45")

    # State
    stage_index: int = 0
    solved: bool = False
    failed: bool = False
    error: str | None = None
    pin_access: PinAccessPlan | None = None
    capacity_mesh: CapacityMesh | None = None
    routing_plan: GlobalRoutePlan | None = None
    result: RouteResult | None = None
    stage_log: list[PipelineStageResult] = field(default_factory=list)
    via_profile_report: dict[str, Any] | None = None

    STAGES: tuple[str, ...] = (
        "via_profile",
        "pin_access",
        "topology_mesh",
        "global_sections",
        "detailed_route",
        "manufacturing_gate",
    )

    def __post_init__(self) -> None:
        self.rules = self.rules or default_design_rules()
        if self.board.copper_layers:
            self.rules = self.rules.model_copy(
                update={"copper_layers": list(self.board.copper_layers)}
            )

    @property
    def stage_name(self) -> str:
        if self.solved:
            return "done"
        if self.failed:
            return "failed"
        if self.stage_index >= len(self.STAGES):
            return "done"
        return self.STAGES[self.stage_index]

    def step(self) -> bool:
        """Run one pipeline stage. Returns True if more work remains."""
        if self.solved or self.failed:
            return False
        if self.stage_index >= len(self.STAGES):
            self.solved = True
            return False
        name = self.STAGES[self.stage_index]
        import time as _time

        t0 = _time.time()
        try:
            if name == "via_profile":
                self._step_via_profile()
            elif name == "pin_access":
                self._step_pin_access()
            elif name == "topology_mesh":
                self._step_topology_mesh()
            elif name == "global_sections":
                self._step_global_sections()
            elif name == "detailed_route":
                self._step_detailed()
            elif name == "manufacturing_gate":
                self._step_manufacturing_gate()
            else:
                raise RuntimeError(f"unknown stage {name}")
        except Exception as exc:  # noqa: BLE001 — surface to failed flag
            self.failed = True
            self.error = f"{name}: {exc}"
            self.stage_log.append(
                PipelineStageResult(
                    name=name,
                    ok=False,
                    detail={"error": str(exc), "elapsed_s": round(_time.time() - t0, 3)},
                )
            )
            return False
        # Annotate last stage with wall time for future diagnostics logs
        if self.stage_log and self.stage_log[-1].name == name:
            detail = dict(self.stage_log[-1].detail or {})
            detail["elapsed_s"] = round(_time.time() - t0, 3)
            self.stage_log[-1] = PipelineStageResult(
                name=name, ok=self.stage_log[-1].ok, detail=detail
            )
        self.stage_index += 1
        if self.stage_index >= len(self.STAGES) and not self.failed:
            self.solved = True
        return not self.solved and not self.failed

    def run(self) -> RouteResult:
        """Drive to completion; raises RuntimeError if failed."""
        while self.step():
            pass
        if self.failed or self.result is None:
            raise RuntimeError(self.error or "route pipeline failed")
        return self.result

    def _progress(self, stage: str, detail: dict[str, Any] | None = None) -> None:
        if not self.progress_cb:
            return
        try:
            self.progress_cb(
                self.stage_index,
                len(self.STAGES),
                stage,
                "pipeline",
                detail or {},
            )
        except Exception:
            pass

    def _step_via_profile(self) -> None:
        """Auto-select via diameter by pin-access reachability (physics cliff)."""
        self._progress("via_profile")
        assert self.rules is not None
        if not self.auto_via_profile:
            self.via_profile_report = {"skipped": True, "reason": "auto_via_profile=False"}
            self.stage_log.append(
                PipelineStageResult(name="via_profile", ok=True, detail=self.via_profile_report)
            )
            return
        from physics_router.pin_access import auto_select_via_profile

        self.rules, self.via_profile_report = auto_select_via_profile(
            self.board, self.rules, profiles=self.via_profiles
        )
        self.stage_log.append(
            PipelineStageResult(
                name="via_profile",
                ok=True,
                detail=dict(self.via_profile_report or {}),
            )
        )

    def _step_pin_access(self) -> None:
        self._progress("pin_access")
        assert self.rules is not None
        self.pin_access = build_pin_access_plan(self.board, self.rules)
        detail = (
            self.pin_access.to_dict() if hasattr(self.pin_access, "to_dict") else {}
        )
        if self.via_profile_report:
            detail = dict(detail)
            detail["via_profile"] = self.via_profile_report
        self.stage_log.append(
            PipelineStageResult(
                name="pin_access",
                ok=True,
                detail=detail,
            )
        )

    def _step_topology_mesh(self) -> None:
        self._progress("topology_mesh")
        assert self.rules is not None
        assert self.pin_access is not None
        # Targets for mesh refinement
        targets: list[tuple[float, float, str]] = []
        topo = plan_graph_topology(
            self.board, self.config, layers=list(self.board.copper_layers or ["F.Cu", "B.Cu"])
        )
        for net, he in topo.hyperedges.items():
            for v in he.vertices:
                targets.append((v.x, v.y, net))
        self.capacity_mesh = build_capacity_mesh(
            self.board,
            self.rules,
            capacity_depth=self.capacity_depth,
            effort=self.effort,
            targets=targets,
        )
        # Mesh-informed cell size for global section negotiation
        leaf_w = (
            sum(n.width for n in self.capacity_mesh.nodes)
            / max(1, len(self.capacity_mesh.nodes))
            if self.capacity_mesh.nodes
            else 1.0
        )
        self.routing_plan = build_global_route_plan(
            self.board,
            self.config,
            self.rules,
            self.pin_access,
            cell_mm=max(0.45, 0.55 * leaf_w),
            capacity_mesh=self.capacity_mesh,
        )
        self.stage_log.append(
            PipelineStageResult(
                name="topology_mesh",
                ok=True,
                detail={
                    "mesh": self.capacity_mesh.to_dict(),
                    "topology_nets": len(topo.hyperedges),
                },
            )
        )

    def _step_global_sections(self) -> None:
        self._progress("global_sections")
        assert self.routing_plan is not None
        # Optional: annotate mesh path lengths into metrics
        mesh = self.capacity_mesh
        if mesh is not None:
            occ: dict[str, float] = {}
            hist: dict[str, float] = {}
            paths_ok = 0
            for net, sections in self.routing_plan.sections.items():
                verts = self.routing_plan.topology.hyperedges[net].vertices
                for sec in sections:
                    s = verts[sec.u]
                    g = verts[sec.v]
                    path = path_through_mesh(
                        mesh, (s.x, s.y), (g.x, g.y), occupancy=occ, history=hist
                    )
                    if path:
                        paths_ok += 1
                        for nid in path:
                            occ[nid] = occ.get(nid, 0.0) + 1.0
                            node = mesh.node_map().get(nid)
                            if node and occ[nid] > node.capacity:
                                hist[nid] = hist.get(nid, 0.0) + 2.0
            self.routing_plan.metrics["mesh_paths_ok"] = paths_ok
            self.routing_plan.metrics["mesh_overflow_nodes"] = sum(
                1
                for nid, dem in occ.items()
                if dem > (mesh.node_map().get(nid).capacity if mesh.node_map().get(nid) else 1)
            )
        self.stage_log.append(
            PipelineStageResult(
                name="global_sections",
                ok=True,
                detail=self.routing_plan.to_dict().get("metrics", {}),
            )
        )

    def _step_detailed(self) -> None:
        self._progress("detailed_route")
        from physics_router.hybrid_route import hybrid_route

        assert self.rules is not None
        self.result = hybrid_route(
            self.board,
            self.config,
            self.rules,
            progress_cb=self.progress_cb,
            routing_plan=self.routing_plan,
        )
        detail = {
            "segments": len(self.result.segments),
            "vias": self.result.via_count,
            "unrouted": list(self.result.unrouted_nets),
            # Sample of copper for live UI canvas (cap size)
            "segment_samples": [
                {
                    "x1": s.x1,
                    "y1": s.y1,
                    "x2": s.x2,
                    "y2": s.y2,
                    "layer": s.layer,
                    "net": s.net,
                    "width_mm": s.width_mm,
                }
                for s in (self.result.segments or [])[:2000]
            ],
        }
        self._progress("detailed_route", detail)
        self.stage_log.append(
            PipelineStageResult(
                name="detailed_route",
                ok=True,
                detail={
                    "segments": len(self.result.segments),
                    "vias": self.result.via_count,
                    "unrouted": list(self.result.unrouted_nets),
                },
            )
        )

    def _step_manufacturing_gate(self) -> None:
        self._progress("manufacturing_gate")
        assert self.result is not None
        assert self.rules is not None
        # Hybrid already ran completion_recovery; only recount here.
        open_before = [
            n
            for n in self.board.nets
            if not _net_fully_connected(
                self.board,
                n,
                self.result.segments,
                self.result.vias,
                areas=getattr(self.result, "areas", None) or [],
            )
        ]

        complete = {
            net
            for net in self.board.nets
            if _net_fully_connected(
                self.board,
                net,
                self.result.segments,
                self.result.vias,
                areas=getattr(self.result, "areas", None) or [],
            )
        }
        cl = float(self.rules.constraints.min_clearance_mm)
        rep = native_drc_check(self.result, clearance_mm=cl, board=self.board)
        violations = int(rep.get("violations") or 0)
        passed = len(complete) == len(self.board.nets) and violations == 0
        # Partial-ok policy: zero hard DRC is a soft pass for golden partial boards
        soft_ok = violations == 0
        gate = {
            "passed": passed,
            "status": "native_candidate"
            if passed
            else ("partial_legal" if soft_ok else "failed"),
            "complete_nets": len(complete),
            "required_nets": len(self.board.nets),
            "unrouted_nets": sorted(set(self.board.nets) - complete),
            "native_drc_violations": violations,
            "shorts": int(rep.get("shorts") or 0),
            "kicad_drc_required": True,
            "stages": [s.name for s in self.stage_log],
            "capacity_mesh": self.capacity_mesh.to_dict() if self.capacity_mesh else None,
            "via_profile": self.via_profile_report,
            "shared_escape": (self.routing_plan.metrics.get("shared_escape") if self.routing_plan else None),
            "open_before_recovery": len(open_before),
        }
        q = dict(self.result.quality or {})
        q["manufacturing_gate"] = gate
        if self.via_profile_report:
            q["via_profile"] = self.via_profile_report
        if self.routing_plan and self.routing_plan.metrics.get("shared_escape"):
            q["shared_escape"] = self.routing_plan.metrics["shared_escape"]
        q["pipeline"] = "capacity_mesh+hybrid"
        if self.routing_plan is not None:
            q["production_route_plan"] = self.routing_plan.to_dict()
        self.result.quality = q
        if not passed:
            self.result.notes.append(
                "ROUTE FAILED manufacturing gate: "
                f"{len(complete)}/{len(self.board.nets)} complete nets, "
                f"{violations} native DRC violation(s)"
            )
            # Fail loud only on hard DRC; partial legal routes still return copper
            if not soft_ok:
                self.failed = True
                self.error = (
                    f"manufacturing gate failed: {len(complete)}/{len(self.board.nets)} "
                    f"nets, {violations} DRC"
                )
            else:
                self.failed = False
                self.error = None
        self.stage_log.append(
            PipelineStageResult(name="manufacturing_gate", ok=passed or soft_ok, detail=gate)
        )


def run_capacity_pipeline(
    board: BoardModel,
    config: PlacementConfig | None = None,
    rules: DesignRules | None = None,
    *,
    effort: float = 0.55,
    capacity_depth: int | None = None,
    progress_cb: ProgressCallback | None = None,
    raise_on_fail: bool = False,
    auto_via_profile: bool = True,
    via_profiles: tuple[str, ...] = ("via_0p6", "via_0p45"),
) -> RouteResult:
    """Convenience entry: full capacity-mesh pipeline → copper."""
    solver = RoutePipelineSolver(
        board=board,
        config=config,
        rules=rules,
        effort=effort,
        capacity_depth=capacity_depth,
        progress_cb=progress_cb,
        auto_via_profile=auto_via_profile,
        via_profiles=via_profiles,
    )
    while solver.step():
        pass
    if solver.result is None:
        raise RuntimeError(solver.error or "pipeline produced no result")
    if raise_on_fail and solver.failed:
        raise RuntimeError(solver.error or "manufacturing gate failed")
    return solver.result
