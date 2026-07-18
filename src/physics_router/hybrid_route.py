"""Multi-strategy hybrid routing with auto net classification (no ring geometry).

Nets are bucketed into free-angle strategies that share one painted obstacle map
so clearance, track width, layer policy, and weights stay consistent.

Strategies
----------
- ``power``    — wider copper, plane-preferring free-angle / native
- ``critical`` — high-weight / high-speed / clock / RF free-angle with vias
- ``matrix``   — dense multipin buses (e.g. CPX-*) with layer striping + vias
- ``general``  — remaining signals free-angle isotropic

See docs/HYBRID_ROUTING.md.
"""

from __future__ import annotations

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

# Matrix (dense multipin) first so later power/critical see painted copper
_STRATEGY_ORDER = ("matrix", "power", "critical", "general")


@dataclass
class NetAssignment:
    net: str
    strategy: str
    reason: str
    width_mm: float
    clearance_mm: float
    layers: list[str] = field(default_factory=list)
    weight: float = 1.0
    region: str = "board"


@dataclass
class HybridPlan:
    assignments: list[NetAssignment]
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


def _matrix_name(net: str) -> bool:
    u = net.upper()
    return bool(re.match(r"^CPX[-_]?\d+$", u)) or u.startswith("MATRIX")


def classify_board(
    board: BoardModel,
    config: PlacementConfig | None = None,
    rules: DesignRules | None = None,
) -> HybridPlan:
    """Auto-assign each net to a free-angle routing strategy + constraints."""
    rules = rules or default_design_rules()
    if board.copper_layers:
        rules = rules.model_copy(update={"copper_layers": list(board.copper_layers)})

    plan = HybridPlan(assignments=[])

    for net in sorted(board.nets.keys()):
        lab = config.net_by_name().get(net) if config else None
        w = rules.track_width_for_net(net, config)
        cl = rules.clearance_for_net(net, config)
        layers = rules.layers_for_net(net, config)
        weight = config.weight_for_net(net) if config else 1.0
        pins = len(board.nets.get(net) or [])

        strategy = "general"
        reason = "default free-angle"
        nu = net.upper()

        # Power / ground first (even if multipin) — wider copper, plane layers
        if lab and lab.net_class in (NetClass.POWER, NetClass.GROUND):
            strategy = "power"
            reason = f"net_class={lab.net_class.value}"
            w = max(w, 0.4 if lab.net_class == NetClass.POWER else 0.3)
        elif any(k in nu for k in ("VCC", "VDD", "+3V", "+5V", "VBAT", "GND", "VSS", "PGND")):
            strategy = "power"
            reason = "name heuristic power/gnd"
            w = max(w, 0.3)
        elif _matrix_name(net) or (pins >= 12 and not (lab and lab.net_class in (NetClass.POWER, NetClass.GROUND))):
            strategy = "matrix"
            reason = f"dense multipin bus pins={pins}" if pins >= 12 else "charlieplex/matrix name"
            w = min(w, max(rules.constraints.min_track_width_mm, 0.2))
        elif lab and (
            lab.critical
            or lab.net_class
            in (
                NetClass.HIGH_SPEED,
                NetClass.DIFFERENTIAL,
                NetClass.CLOCK,
                NetClass.RF,
                NetClass.ANALOG,
            )
        ):
            strategy = "critical"
            reason = f"critical/HS class={lab.net_class.value} weight={weight:.2f}"
        elif lab and weight >= 2.0:
            strategy = "critical"
            reason = f"high weight={weight:.2f}"
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

    clears = [
        plan.assignment(n).clearance_mm  # type: ignore[union-attr]
        for n in nets
        if plan.assignment(n) is not None
    ]
    cl = max(rules.constraints.min_clearance_mm, min(clears) if clears else 0.2)

    # matrix: finer grid + always allow vias for layer escapes
    # power: coarser grid, wider via
    # critical: fine grid
    if strategy == "matrix":
        grid = 0.15
    elif strategy == "power":
        grid = 0.25
    elif strategy == "critical":
        grid = 0.15
    else:
        grid = 0.2

    # Always prefer C++ core for the search hot path (phase-level progress only)
    prefer_native = True

    # Within matrix, few-pin first within priority (less blockage early)
    order = sorted(
        nets,
        key=lambda n: (
            -plan.assignment(n).weight if plan.assignment(n) else 0,  # type: ignore[union-attr]
            len(board.nets.get(n, [])),
            n,
        ),
    )

    r = clearance_aware_route(
        board,
        config,
        clearance_mm=cl,
        grid_mm=grid,
        soft_fallback=False,
        prefer_native=prefer_native,
        allow_vias=True,
        net_order=order,
        nets_filter=nets,
        seed_result=seed,
        style="isotropic",
        design_rules=rules,
        # No per-net progress_cb so native C++ early path is not disabled
        progress_cb=None,
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
    """Route with auto-selected free-angle strategies per net class."""
    rules = rules or default_design_rules()
    if board.copper_layers:
        rules = rules.model_copy(update={"copper_layers": list(board.copper_layers)})
    if clearance_mm is not None:
        c = rules.constraints.model_copy()
        c.min_clearance_mm = max(c.min_clearance_mm, float(clearance_mm))
        rules = rules.model_copy(update={"constraints": c})

    plan = plan or classify_board(board, config, rules)
    result = RouteResult()
    result.notes.append("pipeline: hybrid multi-strategy (topological free-angle)")
    result.notes.append(
        "policy: sequential zero-violation per phase (priority/weight, rip-up, open>short)"
    )
    result.notes.extend(plan.notes)

    phases = [s for s in _STRATEGY_ORDER if plan.nets_for(s)]
    phase_n = max(1, len(phases))

    for pi, strategy in enumerate(phases):
        nets = plan.nets_for(strategy)
        if not nets:
            continue
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

    cl_floor = rules.constraints.min_clearance_mm
    layers = list(board.copper_layers) or ["F.Cu", "B.Cu"]
    if result.segments:
        # Residual safety only — phases already gate at zero violations
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
        # Last resort: drop shorting copper (open > short); never leave shorts
        result = purge_shorting_copper(result, board, config, clearance_mm=cl_floor)

    attach_router_drc(result, clearance_mm=cl_floor, board=board)
    # Honesty: if any short remains, purge harder until clean
    drc = (result.quality or {}).get("drc") or {}
    if int(drc.get("shorts") or 0) > 0:
        result = purge_shorting_copper(
            result, board, config, clearance_mm=cl_floor, max_passes=200
        )
        attach_router_drc(result, clearance_mm=cl_floor, board=board)
    result.compute_quality()
    q = result.quality or {}
    q["pipeline"] = "hybrid"
    q["hybrid_plan"] = plan.to_dict()
    strat_by_net = {a.net: a.strategy for a in plan.assignments}
    for rep in result.net_reports:
        rep.notes = list(rep.notes or [])
        if rep.net in strat_by_net:
            tag = f"strategy={strat_by_net[rep.net]}"
            if tag not in rep.notes:
                rep.notes.append(tag)
    result.quality = q
    result.notes.append(q.get("summary", ""))
    return result
