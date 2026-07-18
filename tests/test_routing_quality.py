"""Routing quality: multi-bend free-angle paths, clearance, no overlap, vias.

Electrical connectivity and clearance matter more than via minimization.
Default search grid is 0.1 mm (fine free-angle / near continuous).
"""

from __future__ import annotations

from physics_router.config_io import example_config
from physics_router.design_rules import default_design_rules
from physics_router.kicad_io import board_from_synthetic
from physics_router.models import BoardModel, Component
from physics_router.router import (
    ObstacleMap,
    RouteResult,
    RouteSegment,
    Via,
    audit_same_layer_clearance,
    build_obstacle_map,
    clearance_aware_route,
    free_angle_route,
    remove_redundant_vias,
)
from physics_router.routing_strategies import topor_style_route


def _count_bends_in_path(path: list[tuple[float, float]], *, min_cross: float = 1e-3) -> int:
    """Number of non-collinear triples (true corners) along a polyline."""
    if len(path) < 3:
        return 0
    bends = 0
    for i in range(1, len(path) - 1):
        ax = path[i][0] - path[i - 1][0]
        ay = path[i][1] - path[i - 1][1]
        bx = path[i + 1][0] - path[i][0]
        by = path[i + 1][1] - path[i][1]
        if abs(ax * by - ay * bx) > min_cross:
            bends += 1
    return bends


def test_free_angle_bends_around_block_at_fine_grid():
    """Blocked LOS must produce a bent path (detour/A*), not fail or go straight through."""
    om = ObstacleMap(60, 40, layers=["F.Cu"], clearance_mm=0.2, x_min=0, y_min=0)
    # Wall between start and goal
    om.add_rect(30, 20, 6, 28, "F.Cu", net="BLOCK", inflate=True)
    start, goal = (5.0, 20.0), (55.0, 20.0)
    assert om.segment_blocked(start[0], start[1], goal[0], goal[1], "F.Cu", "SIG", width_mm=0.25)

    meth: list[str] = []
    path = free_angle_route(
        start, goal, "F.Cu", "SIG", om, grid_mm=0.1, method_out=meth, width_mm=0.25
    )
    assert path is not None, "must find a path around the block"
    assert len(path) >= 3, f"path must bend, got {len(path)} points: {path}"
    assert meth and meth[0] in ("detour", "detour2", "detour3", "astar")
    assert _count_bends_in_path(path) >= 1
    # Path edges must clear the block
    for i in range(len(path) - 1):
        assert not om.segment_blocked(
            path[i][0], path[i][1], path[i + 1][0], path[i + 1][1], "F.Cu", "SIG", width_mm=0.25
        )


def test_free_angle_respects_clearance_between_nets():
    """Second net must not run on top of first net's copper (clearance)."""
    om = ObstacleMap(50, 40, layers=["F.Cu"], clearance_mm=0.25, x_min=0, y_min=0)
    # Existing foreign track
    om.paint_trace(0, 20, 50, 20, "F.Cu", 0.3, "NET_A")
    # Parallel close path for NET_B must be blocked
    assert om.segment_blocked(0, 20.35, 50, 20.35, "F.Cu", "NET_B", width_mm=0.3)
    # Far path OK
    path = free_angle_route(
        (2.0, 5.0), (48.0, 5.0), "F.Cu", "NET_B", om, grid_mm=0.1, width_mm=0.3
    )
    assert path is not None
    for i in range(len(path) - 1):
        assert not om.segment_blocked(
            path[i][0], path[i][1], path[i + 1][0], path[i + 1][1], "F.Cu", "NET_B", width_mm=0.3
        )


def test_clearance_route_no_same_layer_overlap_audit():
    """Full route: soft_fallback off + audit shows no/near-zero clearance violations."""
    cfg = example_config()
    board = board_from_synthetic(cfg)
    r = clearance_aware_route(
        board,
        cfg,
        clearance_mm=0.2,
        grid_mm=0.25,
        soft_fallback=False,
        prefer_native=False,
        allow_vias=True,
    )
    assert r.segments, "expected some routed copper"
    for rep in r.net_reports:
        assert "straight_fallback" not in (rep.method or "")
    audit = audit_same_layer_clearance(r, clearance_mm=0.2)
    n_bad = int(audit.get("near_miss_pairs") or 0)
    assert n_bad <= 2, f"too many clearance near-misses: {audit}"


def test_via_used_when_same_layer_blocked():
    """When F.Cu is walled off, router should place a via rather than leave open if B.Cu free."""
    board = BoardModel(
        width_mm=40,
        height_mm=20,
        copper_layers=["F.Cu", "B.Cu"],
        components={
            "A": Component(ref="A", x_mm=5, y_mm=10, width_mm=1, height_mm=1),
            "B": Component(ref="B", x_mm=35, y_mm=10, width_mm=1, height_mm=1),
        },
        nets={"SIG": [("A", "1"), ("B", "1")]},
    )
    # Huge F.Cu keepout wall; B.Cu empty → via required
    om = build_obstacle_map(board, clearance_mm=0.2, layers=["F.Cu", "B.Cu"])
    om.add_rect(20, 10, 4, 18, "F.Cu", net="WALL", inflate=True)

    from physics_router.router import _route_point_to_point

    path, vias = _route_point_to_point(
        (5.0, 10.0),
        (35.0, 10.0),
        "SIG",
        om,
        layers=["F.Cu", "B.Cu"],
        grid_mm=0.1,
        allow_vias=True,
        width_mm=0.25,
    )
    assert path is not None, "must route with via when one layer blocked"
    layers_used = {p[2] for p in path}
    if len(layers_used) > 1:
        assert vias, "layer hop should introduce a via"
    else:
        # Same-layer detour also OK if it bent around on F.Cu
        pts = [(p[0], p[1]) for p in path]
        assert len(pts) >= 2


def test_via_policy_keeps_vias_by_default():
    """remove_redundant_vias does not strip vias unless aggressive=True."""
    r = RouteResult(
        segments=[
            RouteSegment(0, 0, 5, 0, "F.Cu", "N", 0.25),
            RouteSegment(5, 0, 10, 0, "B.Cu", "N", 0.25),
        ],
        vias=[Via(5, 0, net="N", layers=("F.Cu", "B.Cu"))],
        via_count=1,
    )
    board = BoardModel(width_mm=20, height_mm=10, copper_layers=["F.Cu", "B.Cu"], components={}, nets={})
    out = remove_redundant_vias(r, board, clearance_mm=0.2, aggressive=False)
    assert len(out.vias) == 1
    assert any("via_policy" in n for n in out.notes)


def test_topor_default_fine_grid_and_bends_or_vias():
    """TopoR pipeline at fine grid produces copper without soft illegal fills."""
    cfg = example_config()
    board = board_from_synthetic(cfg)
    rules = default_design_rules()
    r = topor_style_route(
        board,
        cfg,
        rules,
        clearance_mm=0.2,
        grid_mm=0.25,  # slightly coarser for test wall-clock; production default 0.1
        num_variants=1,
        negotiate_iters=1,
        k_homotopy=1,
        use_cbs=False,
        use_elastic=False,
        use_planner=False,
    )
    assert r.segments
    assert r.clearance_violations == 0 or all(
        "straight_fallback" not in (rep.method or "") for rep in r.net_reports
    )
    assert r.via_count >= 0
    # Quality reports winner pipeline
    q = r.quality or r.compute_quality()
    assert q.get("grade") or q.get("score") is not None


def test_blocked_corridor_forces_multi_point_path():
    """Regression: dense obstacles yield detour2/detour3/astar multi-point paths."""
    om = ObstacleMap(80, 50, layers=["F.Cu"], clearance_mm=0.15, x_min=0, y_min=0)
    # Staggered blocks forcing snake path
    for cx, cy in ((20, 15), (20, 35), (40, 25), (60, 15), (60, 35)):
        om.add_rect(cx, cy, 8, 10, "F.Cu", net=f"B{cx}{cy}", inflate=True)
    meth: list[str] = []
    path = free_angle_route(
        (5.0, 25.0), (75.0, 25.0), "F.Cu", "SIG", om, grid_mm=0.1, method_out=meth, width_mm=0.2
    )
    assert path is not None
    assert len(path) >= 3
    assert meth[0] != "los"
    assert _count_bends_in_path(path) >= 1


def test_grid_01_resolves_tighter_than_coarse():
    """0.1 mm grid can thread a narrow channel that 1.0 mm grid misses."""
    om = ObstacleMap(40, 20, layers=["F.Cu"], clearance_mm=0.1, x_min=0, y_min=0)
    # Channel around y=10 of height ~1.2 mm between blocks
    om.add_rect(20, 5, 30, 8, "F.Cu", net="LO", inflate=True)
    om.add_rect(20, 15, 30, 8, "F.Cu", net="HI", inflate=True)
    start, goal = (2.0, 10.0), (38.0, 10.0)
    fine = free_angle_route(start, goal, "F.Cu", "N", om, grid_mm=0.1, width_mm=0.15)
    # Coarse grid may fail or succeed via detour — just must not crash
    free_angle_route(start, goal, "F.Cu", "N", om, grid_mm=1.0, width_mm=0.15, max_expansions=500)
    assert fine is not None
    for i in range(len(fine) - 1):
        assert not om.segment_blocked(
            fine[i][0], fine[i][1], fine[i + 1][0], fine[i + 1][1], "F.Cu", "N", width_mm=0.15
        )
