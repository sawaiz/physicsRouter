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

    STAGES: tuple[str, ...] = (
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
        try:
            if name == "pin_access":
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
                PipelineStageResult(name=name, ok=False, detail={"error": str(exc)})
            )
            return False
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

    def _step_pin_access(self) -> None:
        self._progress("pin_access")
        assert self.rules is not None
        self.pin_access = build_pin_access_plan(self.board, self.rules)
        self.stage_log.append(
            PipelineStageResult(
                name="pin_access",
                ok=True,
                detail=self.pin_access.to_dict() if hasattr(self.pin_access, "to_dict") else {},
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
        gate = {
            "passed": passed,
            "status": "native_candidate" if passed else "failed",
            "complete_nets": len(complete),
            "required_nets": len(self.board.nets),
            "unrouted_nets": sorted(set(self.board.nets) - complete),
            "native_drc_violations": violations,
            "shorts": int(rep.get("shorts") or 0),
            "kicad_drc_required": True,
            "stages": [s.name for s in self.stage_log],
            "capacity_mesh": self.capacity_mesh.to_dict() if self.capacity_mesh else None,
        }
        q = dict(self.result.quality or {})
        q["manufacturing_gate"] = gate
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
            # Fail loud: mark pipeline failed (tscircuit policy: no silent pass)
            self.failed = True
            self.error = (
                f"manufacturing gate failed: {len(complete)}/{len(self.board.nets)} "
                f"nets, {violations} DRC"
            )
        self.stage_log.append(
            PipelineStageResult(name="manufacturing_gate", ok=passed, detail=gate)
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
) -> RouteResult:
    """Convenience entry: full capacity-mesh pipeline → copper."""
    solver = RoutePipelineSolver(
        board=board,
        config=config,
        rules=rules,
        effort=effort,
        capacity_depth=capacity_depth,
        progress_cb=progress_cb,
    )
    while solver.step():
        pass
    if solver.result is None:
        raise RuntimeError(solver.error or "pipeline produced no result")
    if raise_on_fail and solver.failed:
        raise RuntimeError(solver.error or "manufacturing gate failed")
    return solver.result
