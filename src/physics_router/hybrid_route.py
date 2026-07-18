"""Multi-strategy hybrid routing with auto region/net detection.

Different nets (and board regions) can use different algorithms while sharing
one painted obstacle map so **clearance, track width, layer policy, and net
weights** stay consistent with design rules.

Strategies
----------
- ``ring``     — concentric polar tracks (charlieplex LED rings / halo.js)
- ``power``    — wider copper, plane-preferring free-angle / native
- ``critical`` — high-weight / high-speed / clock / RF free-angle with vias
- ``general``  — remaining signals free-angle isotropic

Detection is automatic from geometry (LED ring), net names (CPX-*), and
PlacementConfig net classes / weights. See docs/HYBRID_ROUTING.md.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from physics_router.design_rules import DesignRules, default_design_rules
from physics_router.models import BoardModel, NetClass, PlacementConfig
from physics_router.router import (
    RouteResult,
    attach_router_drc,
    clearance_aware_route,
    purge_shorting_copper,
    repair_drc_conflicts,
)

ProgressCallback = Callable[[int, int, str, str, dict], None]

# Paint order: densest / most geometric first, then power, then critical, then rest
_STRATEGY_ORDER = ("ring", "power", "critical", "general")


@dataclass
class NetAssignment:
    net: str
    strategy: str
    reason: str
    width_mm: float
    clearance_mm: float
    layers: list[str] = field(default_factory=list)
    weight: float = 1.0
    region: str = "board"  # ring | core | board


@dataclass
class HybridPlan:
    assignments: list[NetAssignment]
    has_ring: bool = False
    ring_summary: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def nets_for(self, strategy: str) -> list[str]:
        return [a.net for a in self.assignments if a.strategy == strategy]

    def assignment(self, net: str) -> NetAssignment | None:
        for a in self.assignments:
            if a.net == net:
                return a
        return None

    def to_dict(self) -> dict[str, Any]:
        by: dict[str, list[str]] = {}
        for a in self.assignments:
            by.setdefault(a.strategy, []).append(a.net)
        return {
            "has_ring": self.has_ring,
            "ring": self.ring_summary,
            "by_strategy": {k: sorted(v) for k, v in by.items()},
            "counts": {k: len(v) for k, v in by.items()},
            "assignments": [
                {
                    "net": a.net,
                    "strategy": a.strategy,
                    "reason": a.reason,
                    "width_mm": a.width_mm,
                    "clearance_mm": a.clearance_mm,
                    "layers": a.layers,
                    "weight": a.weight,
                    "region": a.region,
                }
                for a in self.assignments
            ],
            "notes": self.notes,
        }


def _cpx_name(net: str) -> bool:
    return bool(re.match(r"^CPX[-_]?\d+$", net.upper()))


def _net_region(
    board: BoardModel,
    net: str,
    *,
    ring_cx: float,
    ring_cy: float,
    ring_r: float,
) -> str:
    """Classify where net pins live: ring annulus, core, or mixed board."""
    pins = board.nets.get(net) or []
    if not pins:
        return "board"
    rs: list[float] = []
    for ref, _pad in pins:
        c = board.components.get(ref)
        if c is None:
            continue
        rs.append(math.hypot(c.x_mm - ring_cx, c.y_mm - ring_cy))
    if not rs:
        return "board"
    on_ring = sum(1 for r in rs if abs(r - ring_r) < 0.15 * max(ring_r, 1.0))
    in_core = sum(1 for r in rs if r < 0.55 * ring_r)
    if on_ring >= max(1, int(0.5 * len(rs))):
        return "ring"
    if in_core >= max(1, int(0.5 * len(rs))):
        return "core"
    return "board"


def classify_board(
    board: BoardModel,
    config: PlacementConfig | None = None,
    rules: DesignRules | None = None,
) -> HybridPlan:
    """Auto-assign each net to a routing strategy + geometry constraints."""
    rules = rules or default_design_rules()
    # Prefer board copper order
    if board.copper_layers:
        rules = rules.model_copy(update={"copper_layers": list(board.copper_layers)})

    ring = None
    try:
        from physics_router.halo_ring import detect_led_ring

        ring = detect_led_ring(board)
    except Exception:
        ring = None

    plan = HybridPlan(assignments=[], has_ring=ring is not None)
    if ring is not None:
        plan.ring_summary = {
            "cx": ring.cx,
            "cy": ring.cy,
            "radius": ring.radius,
            "leds": len(ring.led_refs),
        }
        plan.notes.append(
            f"region: LED ring R≈{ring.radius:.2f}mm n={len(ring.led_refs)} "
            f"@({ring.cx:.2f},{ring.cy:.2f})"
        )

    for net in sorted(board.nets.keys()):
        lab = config.net_by_name().get(net) if config else None
        w = rules.track_width_for_net(net, config)
        cl = rules.clearance_for_net(net, config)
        layers = rules.layers_for_net(net, config)
        weight = config.weight_for_net(net) if config else 1.0
        region = "board"
        if ring is not None:
            region = _net_region(
                board, net, ring_cx=ring.cx, ring_cy=ring.cy, ring_r=ring.radius
            )

        strategy = "general"
        reason = "default free-angle"

        # 1) Charlieplex matrix on LED ring → concentric polar tracks
        if ring is not None and _cpx_name(net):
            strategy = "ring"
            reason = "charlieplex/LED-ring geometry (concentric tracks)"
            # halo.js-like thin matrix copper unless rules force wider
            w = min(w, max(rules.constraints.min_track_width_mm, 0.128))
            cl = min(cl, max(rules.constraints.min_clearance_mm, 0.128))

        # 2) Power / ground
        if strategy == "general" and lab and lab.net_class in (
            NetClass.POWER,
            NetClass.GROUND,
        ):
            strategy = "power"
            reason = f"net_class={lab.net_class.value} → wide/plane-prefer"
            w = max(w, 0.3)

        # 3) Critical / HS / RF / weighted
        if strategy == "general" and lab:
            if lab.critical or lab.net_class in (
                NetClass.HIGH_SPEED,
                NetClass.DIFFERENTIAL,
                NetClass.CLOCK,
                NetClass.RF,
                NetClass.ANALOG,
            ):
                strategy = "critical"
                reason = (
                    f"critical/HS class={lab.net_class.value} weight={weight:.2f}"
                )
            elif weight >= 2.0:
                strategy = "critical"
                reason = f"high weight={weight:.2f}"

        # Name heuristics when no labels
        if strategy == "general":
            nu = net.upper()
            if any(k in nu for k in ("VCC", "VDD", "+3V", "+5V", "VBAT", "GND", "VSS")):
                strategy = "power"
                reason = "name heuristic power/gnd"
                w = max(w, 0.3)
            elif any(k in nu for k in ("CLK", "XTAL", "USB", "DIFF", "RF", "ANT")):
                strategy = "critical"
                reason = "name heuristic critical"

        plan.assignments.append(
            NetAssignment(
                net=net,
                strategy=strategy,
                reason=reason,
                width_mm=round(w, 4),
                clearance_mm=round(cl, 4),
                layers=list(layers),
                weight=weight,
                region=region,
            )
        )

    counts = {s: len(plan.nets_for(s)) for s in _STRATEGY_ORDER if plan.nets_for(s)}
    plan.notes.append("strategy counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return plan


def _merge_route(dst: RouteResult, src: RouteResult, *, only_nets: set[str] | None = None) -> None:
    for s in src.segments:
        if only_nets is not None and s.net not in only_nets:
            continue
        dst.segments.append(s)
        dst.total_length_mm += math.hypot(s.x2 - s.x1, s.y2 - s.y1)
    for v in src.vias:
        if only_nets is not None and v.net not in only_nets:
            continue
        dst.vias.append(v)
        dst.via_count += 1
    for rep in src.net_reports:
        if only_nets is not None and rep.net not in only_nets:
            continue
        dst.net_reports.append(rep)
    for u in src.unrouted_nets:
        if only_nets is not None and u not in only_nets:
            continue
        if u not in dst.unrouted_nets:
            dst.unrouted_nets.append(u)
    for n in src.notes or []:
        if n not in dst.notes:
            dst.notes.append(n)


def _route_bucket(
    board: BoardModel,
    config: PlacementConfig | None,
    rules: DesignRules,
    nets: list[str],
    *,
    strategy: str,
    seed: RouteResult | None,
    plan: HybridPlan,
    progress_cb: ProgressCallback | None,
    phase_i: int,
    phase_n: int,
) -> RouteResult:
    """Route one strategy bucket; seed prior copper as obstacles."""
    if not nets:
        return RouteResult()

    if progress_cb:
        try:
            progress_cb(
                phase_i,
                phase_n,
                f"phase:{strategy}",
                "start",
                {"nets": nets, "count": len(nets)},
            )
        except Exception:
            pass

    # Global clearance floor for this bucket (per-net floors already in plan)
    clears = [
        plan.assignment(n).clearance_mm  # type: ignore[union-attr]
        for n in nets
        if plan.assignment(n) is not None
    ]
    cl = max(rules.constraints.min_clearance_mm, min(clears) if clears else 0.2)
    # Per-net width override via temporary config is heavy; clearance_aware uses _net_width
    # and design rules when we pass rules through — use max of design rule widths via config labels.

    if strategy == "ring":
        from physics_router.halo_ring import halo_ring_route

        # Only CPX / ring nets — halo_ring already filters CPX; restrict non-cpx
        sub = copy.deepcopy(board)
        sub.nets = {n: board.nets[n] for n in nets if n in board.nets}
        r = halo_ring_route(
            sub,
            config,
            clearance_mm=cl,
            progress_cb=progress_cb,
            route_non_cpx=False,
        )
        r.notes.append(f"hybrid: ring phase nets={len(nets)}")
        return r

    # Free-angle / native for power, critical, general
    prefer_native = strategy in ("power", "general", "critical")
    # Power: slightly coarser grid is fine; critical: finer
    grid = 0.2 if strategy == "power" else 0.15 if strategy == "critical" else 0.2

    r = clearance_aware_route(
        board,
        config,
        clearance_mm=cl,
        grid_mm=grid,
        soft_fallback=False,
        prefer_native=prefer_native and progress_cb is None,
        allow_vias=True,
        net_order=nets,
        nets_filter=nets,
        seed_result=seed,
        style="isotropic",  # force isotropic bucket (no re-entry hybrid)
        design_rules=rules,
        progress_cb=progress_cb,
        skip_hybrid=True,
    )
    r.notes.append(f"hybrid: {strategy} phase nets={len(nets)} cl={cl:.3f} grid={grid}")
    return r


def hybrid_route(
    board: BoardModel,
    config: PlacementConfig | None = None,
    rules: DesignRules | None = None,
    *,
    clearance_mm: float | None = None,
    progress_cb: ProgressCallback | None = None,
    plan: HybridPlan | None = None,
) -> RouteResult:
    """Route the board with auto-selected algorithms per net/region.

    Constraints (width, clearance, layers, weights) come from DesignRules +
    PlacementConfig and are applied uniformly even when algorithms differ.
    """
    rules = rules or default_design_rules()
    if board.copper_layers:
        rules = rules.model_copy(update={"copper_layers": list(board.copper_layers)})
    if clearance_mm is not None:
        # Raise floor without shrinking per-net rules below board min
        c = rules.constraints.model_copy()
        c.min_clearance_mm = max(c.min_clearance_mm, float(clearance_mm))
        rules = rules.model_copy(update={"constraints": c})

    plan = plan or classify_board(board, config, rules)
    result = RouteResult()
    result.notes.append("pipeline: hybrid multi-strategy")
    result.notes.extend(plan.notes)

    phases = [s for s in _STRATEGY_ORDER if plan.nets_for(s)]
    phase_n = max(1, len(phases))

    for pi, strategy in enumerate(phases):
        nets = plan.nets_for(strategy)
        if not nets:
            continue
        # Apply per-net widths onto a light config shim for _net_width fallback:
        # Prefer design_rules path in clearance_aware.
        partial = _route_bucket(
            board,
            config,
            rules,
            nets,
            strategy=strategy,
            seed=result if result.segments else None,
            plan=plan,
            progress_cb=progress_cb,
            phase_i=pi,
            phase_n=phase_n,
        )
        _merge_route(result, partial, only_nets=set(nets))
        result.notes.append(
            f"hybrid phase {strategy}: +{len(partial.segments)} segs "
            f"+{partial.via_count} vias unrouted={len(partial.unrouted_nets)}"
        )

    # Global repair with shared constraints
    cl_floor = rules.constraints.min_clearance_mm
    layers = list(board.copper_layers) or ["F.Cu", "B.Cu"]
    if result.segments:
        result = repair_drc_conflicts(
            result,
            board,
            config,
            clearance_mm=cl_floor,
            grid_mm=0.2,
            layers=layers,
            allow_vias=True,
            max_rounds=3,
        )
        result = purge_shorting_copper(result, board, config, clearance_mm=cl_floor)

    attach_router_drc(result, clearance_mm=cl_floor, board=board)
    result.compute_quality()
    q = result.quality or {}
    q["pipeline"] = "hybrid"
    q["hybrid_plan"] = plan.to_dict()
    # Annotate net reports with strategy
    strat_by_net = {a.net: a.strategy for a in plan.assignments}
    for rep in result.net_reports:
        rep.notes = list(rep.notes or [])
        if rep.net in strat_by_net:
            tag = f"strategy={strat_by_net[rep.net]}"
            if tag not in rep.notes:
                rep.notes.append(tag)
    result.quality = q
    result.notes.append(q.get("summary", ""))
    if progress_cb:
        try:
            progress_cb(
                phase_n,
                phase_n,
                "hybrid",
                "done",
                {
                    "partial": {
                        "segments": [
                            {
                                "net": s.net,
                                "x1": s.x1,
                                "y1": s.y1,
                                "x2": s.x2,
                                "y2": s.y2,
                                "layer": s.layer,
                                "width_mm": s.width_mm,
                            }
                            for s in result.segments
                        ],
                        "vias": [
                            {
                                "net": v.net,
                                "x": v.x,
                                "y": v.y,
                                "size_mm": v.size_mm,
                                "drill_mm": v.drill_mm,
                                "layers": list(v.layers),
                            }
                            for v in result.vias
                        ],
                        "total_length_mm": result.total_length_mm,
                        "via_count": result.via_count,
                        "unrouted_nets": list(result.unrouted_nets),
                        "clearance_violations": result.clearance_violations,
                    }
                },
            )
        except Exception:
            pass
    return result
