"""Board-wide PathFinder-style congestion negotiation.

The detailed router normally commits legal nets greedily.  That is safe, but a
legal early route can permanently consume the only useful homotopy for a dense
peer.  This module separates *candidate discovery* from *legal commit*:

1. route active nets independently against fixed, conflict-free copper;
2. allow those candidates to temporarily share routing resources;
3. add historical cost to overused cells and exact DRC marker locations;
4. reroute only the conflict component (conflict-directed rip-up);
5. legalize the best bounded iteration and retry its victims sequentially.

Every single-net search still uses the C++ exact-geometry core.  Illegal or
partial copper is never returned from the final result.
"""

from __future__ import annotations

import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from physics_router.models import BoardModel, PlacementConfig
from physics_router.router import (
    NetRouteReport,
    RouteResult,
    _drc_hard_items,
    _net_fully_connected,
    _net_priority,
    _rebuild_totals,
    _route_result_from_dict,
    _seed_segs_with_vias,
    _strip_nets_from_result,
    native_drc_check,
)
from physics_router.topology import CongestionMap


ResourceKey = tuple[int, int, str]


@dataclass
class NegotiationStats:
    iteration: int
    active_nets: int
    completed_nets: int
    hard_violations: int
    overused_cells: int
    conflict_nets: int

    def to_dict(self) -> dict[str, int]:
        return {
            "iteration": self.iteration,
            "active_nets": self.active_nets,
            "completed_nets": self.completed_nets,
            "hard_violations": self.hard_violations,
            "overused_cells": self.overused_cells,
            "conflict_nets": self.conflict_nets,
        }


def route_resource_owners(
    result: RouteResult,
    *,
    cell_mm: float,
    clearance_mm: float,
    layers: list[str],
) -> dict[ResourceKey, set[str]]:
    """Rasterize copper capacity resources while retaining owning net names."""
    owners: dict[ResourceKey, set[str]] = defaultdict(set)
    cell = max(0.1, float(cell_mm))

    def paint(x: float, y: float, layer: str, radius: float, net: str) -> None:
        reach = max(0, int(math.ceil(radius / cell)))
        ix0 = int(math.floor(x / cell))
        iy0 = int(math.floor(y / cell))
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                cx = (ix0 + dx + 0.5) * cell
                cy = (iy0 + dy + 0.5) * cell
                if math.hypot(cx - x, cy - y) <= radius + cell * 0.72:
                    owners[(ix0 + dx, iy0 + dy, layer)].add(net)

    for segment in result.segments:
        length = math.hypot(segment.x2 - segment.x1, segment.y2 - segment.y1)
        steps = max(1, int(math.ceil(length / max(cell * 0.45, 0.05))))
        radius = 0.5 * (segment.width_mm + clearance_mm)
        for index in range(steps + 1):
            t = index / steps
            paint(
                segment.x1 + (segment.x2 - segment.x1) * t,
                segment.y1 + (segment.y2 - segment.y1) * t,
                segment.layer,
                radius,
                segment.net,
            )
    for via in result.vias:
        radius = 0.5 * (via.size_mm + clearance_mm)
        for layer in layers:
            paint(via.x, via.y, layer, radius, via.net)
    return dict(owners)


def _conflict_graph(
    result: RouteResult,
    board: BoardModel,
    *,
    clearance_mm: float,
    owners: dict[ResourceKey, set[str]],
) -> tuple[dict[str, set[str]], dict[str, Any]]:
    graph: dict[str, set[str]] = defaultdict(set)
    board_nets = set(board.nets)
    # Coarse overuse feeds history, but only exact geometry/DRC markers decide
    # which nets are ripped. Otherwise a conservative raster cell can keep the
    # entire board active even after its actual conflict component is small.
    _ = owners
    drc = native_drc_check(
        result,
        clearance_mm=clearance_mm,
        max_violations=2000,
        board=board,
    )
    for item in _drc_hard_items(drc):
        left = str(item.get("net_a") or "")
        right = str(item.get("net_b") or "")
        if left in board_nets and right in board_nets and left != right:
            graph[left].add(right)
            graph[right].add(left)
        elif left in board_nets:
            graph.setdefault(left, set())
    return dict(graph), drc


def _complete_nets(result: RouteResult, board: BoardModel) -> set[str]:
    return {
        net
        for net in board.nets
        if _net_fully_connected(
            board,
            net,
            result.segments,
            result.vias,
            areas=result.areas,
        )
    }


def _merge_candidate(base: RouteResult, candidate: RouteResult, net: str) -> None:
    base.segments.extend(segment for segment in candidate.segments if segment.net == net)
    base.vias.extend(via for via in candidate.vias if via.net == net)
    base.areas.extend(area for area in candidate.areas if area.net == net)
    base.net_reports.extend(report for report in candidate.net_reports if report.net == net)
    if net in candidate.unrouted_nets and net not in base.unrouted_nets:
        base.unrouted_nets.append(net)
    _rebuild_totals(base)


def _route_one_candidate(
    net: str,
    board: BoardModel,
    config: PlacementConfig | None,
    rules: Any,
    plan: Any,
    fixed: RouteResult,
    congestion: CongestionMap,
    clearance_mm: float,
    layers: list[str],
) -> RouteResult:
    assignment = plan.assignment(net) if plan is not None else None
    strategy = getattr(assignment, "strategy", "general")
    grid = 0.15 if strategy in ("matrix", "critical") else 0.25 if strategy == "power" else 0.2
    pins = len(board.nets.get(net) or [])
    from physics_router.native_bridge import route_board_native

    raw = route_board_native(
        board,
        config,
        clearance_mm=clearance_mm,
        grid_mm=grid,
        soft_fallback=False,
        allow_vias=True,
        use_gpu=True,
        isotropic=True,
        net_order=[net],
        exclusive_nets=True,
        seed_segments=_seed_segs_with_vias(fixed, None, layers) or None,
        post_rubberband=pins <= 2,
        via_minimize=False,
        max_expansions=min(48000, max(10000, 3200 * pins)),
        use_copper_areas=False,
        congestion=congestion,
    )
    if raw is None:
        return RouteResult(unrouted_nets=[net])
    return _route_result_from_dict(raw)


def _candidate_key(
    result: RouteResult, board: BoardModel, *, clearance_mm: float
) -> tuple[int, int, int, float, float]:
    completed = len(_complete_nets(result, board))
    drc = native_drc_check(
        result, clearance_mm=clearance_mm, max_violations=2000, board=board
    )
    return (
        completed,
        -int(drc.get("violations") or 0),
        -len(result.unrouted_nets),
        -float(result.via_count),
        -float(result.total_length_mm),
    )


def _bump_history(
    congestion: CongestionMap,
    owners: dict[ResourceKey, set[str]],
    drc: dict[str, Any],
) -> None:
    for key in list(congestion.historical):
        congestion.historical[key] *= congestion.historical_decay
        if congestion.historical[key] < 0.05:
            del congestion.historical[key]
    for key, cell_owners in owners.items():
        overuse = max(0, len(cell_owners) - 1)
        if overuse:
            congestion.historical[key] = (
                congestion.historical.get(key, 0.0)
                + congestion.historical_boost * overuse
            )
    for item in _drc_hard_items(drc):
        layer = str(item.get("layer") or "")
        if layer == "via":
            continue
        key = congestion._key(float(item.get("x") or 0.0), float(item.get("y") or 0.0), layer)
        congestion.historical[key] = (
            congestion.historical.get(key, 0.0)
            + 2.0 * congestion.historical_boost
        )


def _legalize(
    candidate: RouteResult,
    board: BoardModel,
    config: PlacementConfig | None,
    *,
    clearance_mm: float,
) -> tuple[RouteResult, list[str]]:
    def routed_nets(result: RouteResult) -> set[str]:
        return (
            {segment.net for segment in result.segments}
            | {via.net for via in result.vias}
            | {area.net for area in result.areas}
        )

    def rip_until_clean(result: RouteResult) -> tuple[RouteResult, list[str]]:
        removed: list[str] = []
        while True:
            drc = native_drc_check(
                result, clearance_mm=clearance_mm, max_violations=4000, board=board
            )
            items = _drc_hard_items(drc)
            if not items:
                return result, removed
            degree: dict[str, int] = defaultdict(int)
            copper_nets = routed_nets(result)
            for item in items:
                values = [
                    str(item.get(key) or "") for key in ("net_a", "net_b")
                ]
                for net in values:
                    if net in copper_nets:
                        degree[net] += 1
            if not degree:
                return result, removed
            victim = min(
                degree,
                key=lambda net: (
                    _net_priority(config, net),
                    -degree[net],
                    len(board.nets.get(net) or []),
                    net,
                ),
            )
            removed.append(victim)
            result = _strip_nets_from_result(result, {victim})

    # Build an exact conflict graph, then evaluate several deterministic
    # maximal-independent-set orders. This preserves far more legal nets than
    # repeatedly deleting the net with the most raw segment-pair markers.
    original_nets = routed_nets(candidate)
    initial_drc = native_drc_check(
        candidate, clearance_mm=clearance_mm, max_violations=4000, board=board
    )
    graph: dict[str, set[str]] = defaultdict(set)
    self_bad: set[str] = set()
    for item in _drc_hard_items(initial_drc):
        left = str(item.get("net_a") or "")
        right = str(item.get("net_b") or "")
        left_routed = left in original_nets
        right_routed = right in original_nets
        if left_routed and right_routed and left != right:
            graph[left].add(right)
            graph[right].add(left)
        elif left_routed:
            self_bad.add(left)
        elif right_routed:
            self_bad.add(right)

    orders = [
        sorted(
            original_nets,
            key=lambda net: (
                -_net_priority(config, net),
                len(graph.get(net, set())),
                len(board.nets.get(net) or []),
                net,
            ),
        ),
        sorted(
            original_nets,
            key=lambda net: (
                len(graph.get(net, set())),
                -_net_priority(config, net),
                len(board.nets.get(net) or []),
                net,
            ),
        ),
        sorted(
            original_nets,
            key=lambda net: (
                len(board.nets.get(net) or []),
                len(graph.get(net, set())),
                -_net_priority(config, net),
                net,
            ),
        ),
    ]
    variants: list[tuple[RouteResult, list[str]]] = []
    for order in orders:
        accepted: set[str] = set()
        for net in order:
            if net in self_bad or graph.get(net, set()) & accepted:
                continue
            accepted.add(net)
        removed = sorted(original_nets - accepted)
        trial = _strip_nets_from_result(candidate, set(removed))
        trial, extra = rip_until_clean(trial)
        variants.append((trial, removed + extra))

    # Retain the direct marker-degree strategy as a fallback order.
    variants.append(rip_until_clean(candidate))

    def legal_key(value: tuple[RouteResult, list[str]]) -> tuple:
        result, _removed = value
        copper_nets = routed_nets(result)
        return (
            len(copper_nets),
            sum(_net_priority(config, net) for net in copper_nets),
            -result.via_count,
            -result.total_length_mm,
        )

    return max(variants, key=legal_key)


def negotiated_congestion_route(
    board: BoardModel,
    config: PlacementConfig | None,
    rules: Any,
    plan: Any,
    initial: RouteResult,
    *,
    clearance_mm: float,
    max_iterations: int = 3,
    workers: int = 4,
) -> RouteResult:
    """Run a bounded board-wide negotiated-congestion and rip-up pass."""
    layers = list(board.copper_layers) or ["F.Cu", "B.Cu"]
    congestion = CongestionMap(
        cell_mm=max(0.25, clearance_mm * 2.0),
        present_weight=1.25,
        historical_weight=1.0,
        historical_decay=0.96,
        historical_boost=max(0.6, clearance_mm * 6.0),
    )
    # Filled areas are authoritative power resources, not temporary signal
    # paths. Keep those nets fixed while all remaining route classes negotiate.
    protected = {area.net for area in initial.areas}
    active = sorted(set(board.nets) - protected)
    working = _strip_nets_from_result(initial, set(active))
    best_legal = initial
    best_iteration = initial
    iteration_stats: list[NegotiationStats] = []

    for iteration in range(max(1, max_iterations)):
        fixed = _strip_nets_from_result(working, set(active))
        ordered_active = sorted(
            active,
            key=lambda net: (
                -_net_priority(config, net),
                len(board.nets.get(net) or []),
                net,
            ),
        )
        worker_count = max(1, min(workers, len(ordered_active) or 1))
        groups = [ordered_active[index::worker_count] for index in range(worker_count)]

        def route_group(group: list[str]) -> list[tuple[str, RouteResult]]:
            local = CongestionMap(
                cell_mm=congestion.cell_mm,
                present_weight=congestion.present_weight,
                historical_weight=congestion.historical_weight,
                historical_decay=congestion.historical_decay,
                historical_boost=congestion.historical_boost,
                historical=dict(congestion.historical),
            )
            routed: list[tuple[str, RouteResult]] = []
            for net in group:
                candidate = _route_one_candidate(
                    net,
                    board,
                    config,
                    rules,
                    plan,
                    fixed,
                    local,
                    clearance_mm,
                    layers,
                )
                routed.append((net, candidate))
                # Present cost is deliberately soft: peer candidates may still
                # share the resource, but later peers prefer another homotopy.
                local.paint_route(candidate, amount=1.5)
            return routed

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            grouped_candidates = list(
                pool.map(
                    route_group,
                    groups,
                )
            )
        candidate_by_net = {
            net: candidate
            for group in grouped_candidates
            for net, candidate in group
        }
        working = fixed
        for net in active:
            candidate = candidate_by_net[net]
            _merge_candidate(working, candidate, net)

        complete = _complete_nets(working, board)
        working.unrouted_nets = sorted(set(board.nets) - complete)
        owners = route_resource_owners(
            working,
            cell_mm=congestion.cell_mm,
            clearance_mm=clearance_mm,
            layers=layers,
        )
        overused = {key: value for key, value in owners.items() if len(value) > 1}
        graph, drc = _conflict_graph(
            working,
            board,
            clearance_mm=clearance_mm,
            owners=overused,
        )
        conflict_nets = (set(graph) | set(working.unrouted_nets)) - protected
        iteration_stats.append(
            NegotiationStats(
                iteration=iteration + 1,
                active_nets=len(active),
                completed_nets=len(complete),
                hard_violations=int(drc.get("violations") or 0),
                overused_cells=len(overused),
                conflict_nets=len(conflict_nets),
            )
        )
        if _candidate_key(working, board, clearance_mm=clearance_mm) > _candidate_key(
            best_iteration, board, clearance_mm=clearance_mm
        ):
            best_iteration = working
        if not conflict_nets and not working.unrouted_nets:
            best_legal = working
            break
        _bump_history(congestion, overused, drc)
        active = sorted(conflict_nets)
        if not active:
            break

    legalized, victims = _legalize(
        best_iteration,
        board,
        config,
        clearance_mm=clearance_mm,
    )
    complete = _complete_nets(legalized, board)
    legalized.unrouted_nets = sorted(set(board.nets) - complete)

    # Conflict-directed final repair: retry only removed/incomplete nets, in
    # priority order, against the now legal committed subset.
    repair_order = sorted(
        set(victims) | set(legalized.unrouted_nets),
        key=lambda net: (-_net_priority(config, net), len(board.nets.get(net) or []), net),
    )
    repaired: list[str] = []
    for net in repair_order:
        trial_net = _route_one_candidate(
            net,
            board,
            config,
            rules,
            plan,
            legalized,
            congestion,
            clearance_mm,
            layers,
        )
        trial = _strip_nets_from_result(legalized, {net})
        _merge_candidate(trial, trial_net, net)
        if not _net_fully_connected(
            board, net, trial.segments, trial.vias, areas=trial.areas
        ):
            continue
        drc = native_drc_check(
            trial, clearance_mm=clearance_mm, max_violations=2000, board=board
        )
        if int(drc.get("violations") or 0) == 0:
            legalized = trial
            repaired.append(net)

    complete = _complete_nets(legalized, board)
    legalized.unrouted_nets = sorted(set(board.nets) - complete)
    if _candidate_key(best_legal, board, clearance_mm=clearance_mm) > _candidate_key(
        legalized, board, clearance_mm=clearance_mm
    ):
        legalized = best_legal

    for net in legalized.unrouted_nets:
        if any(report.net == net for report in legalized.net_reports):
            continue
        legalized.net_reports.append(
            NetRouteReport(
                net=net,
                pins=len(board.nets.get(net) or []),
                status="unrouted",
                method="pathfinder_conflict_victim",
            )
        )
    legalized.notes.append(
        "pathfinder: board-wide historical congestion + conflict-directed rip-up "
        f"iterations={len(iteration_stats)} repaired={len(repaired)}"
    )
    legalized.quality = {
        **(legalized.quality or {}),
        "negotiated_congestion": {
            "algorithm": "pathfinder_history+conflict_directed_ripup",
            "iterations": [value.to_dict() for value in iteration_stats],
            "historical_cells": len(congestion.historical),
            "protected_area_nets": sorted(protected),
            "victims": victims,
            "repaired": repaired,
            "complete_nets": len(_complete_nets(legalized, board)),
        },
    }
    _rebuild_totals(legalized)
    return legalized
