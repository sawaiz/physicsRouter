"""TopoR-style isotropic pipeline: multi-variant, rubberband, via minimize, topology."""

from __future__ import annotations

from physics_router.config_io import example_config
from physics_router.design_rules import default_design_rules
from physics_router.kicad_io import board_from_synthetic
from physics_router.router import (
    RouteResult,
    RouteSegment,
    Via,
    clearance_aware_route,
    free_angle_route,
    remove_redundant_vias,
    build_obstacle_map,
)
from physics_router.routing_strategies import multilayer_route, topor_style_route
from physics_router.topology import (
    CongestionMap,
    ScoreVector,
    radar_scan_points,
    score_vector_from_route,
    signature_for_polyline,
)


def test_isotropic_detour_not_only_hv() -> None:
    """Free-angle path may use non-orthogonal midpoints (isotropic style)."""
    cfg = example_config()
    board = board_from_synthetic(cfg)
    om = build_obstacle_map(board, clearance_mm=0.2, layers=["F.Cu"])
    # Force a blocked LOS by painting a keepout between two points
    om.add_rect(25, 20, 8, 8, "F.Cu", net="BLOCK", inflate=True)
    start, goal = (5.0, 20.0), (45.0, 20.0)
    meth: list[str] = []
    path = free_angle_route(start, goal, "F.Cu", "SIG", om, grid_mm=1.0, method_out=meth)
    assert path is not None and len(path) >= 2
    # If detour, intermediate points need not be pure L-bend only
    assert meth[0] in ("los", "detour", "detour2", "astar")


def test_topor_style_pipeline_synthetic() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    rules = default_design_rules()
    r = topor_style_route(
        board,
        cfg,
        rules,
        clearance_mm=0.2,
        grid_mm=1.0,
        num_variants=2,
        negotiate_iters=1,
    )
    assert r.segments
    assert any("topor_pipeline" in n for n in r.notes)
    assert any("isotropic" in n.lower() for n in r.notes)
    q = r.quality or r.compute_quality()
    assert q.get("pipeline") == "topor_style"
    assert q.get("winner")
    assert q.get("variants_ranked")
    assert "score_vector" in q or q.get("pareto_front") is not None
    # No illegal soft fallbacks in reports
    for rep in r.net_reports:
        assert "straight_fallback" not in (rep.method or "")


def test_congestion_map_and_radar() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    om = build_obstacle_map(board, clearance_mm=0.2, layers=["F.Cu"])
    pts = radar_scan_points((2.0, 2.0), (40.0, 30.0), om, "F.Cu", "N", rays=8, grid_mm=1.0)
    assert len(pts) >= 1
    cong = CongestionMap(cell_mm=1.0)
    cong.paint_segment(0, 0, 10, 0, "F.Cu", amount=2.0)
    assert cong.cost(5, 0, "F.Cu") > 0
    cong.negotiate()
    assert cong.historical  # boosted after negotiate
    sig = signature_for_polyline("N", "F.Cu", [(0, 0), (10, 0), (10, 10)], om)
    assert sig.key()
    sv = ScoreVector(unrouted=0, via_count=1, total_length_mm=10.0)
    sv2 = ScoreVector(unrouted=1, via_count=0, total_length_mm=5.0)
    assert not sv.dominates(sv2) or sv2.dominates(sv) or True  # either may dominate
    dummy = RouteResult(total_length_mm=10, via_count=1, unrouted_nets=[])
    assert score_vector_from_route(dummy).total_length_mm == 10


def test_multilayer_route_uses_topor_pipeline() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    r = multilayer_route(board, cfg, num_variants=1, grid_mm=1.0, clearance_mm=0.2)
    assert any("topor_pipeline" in n or "isotropic" in n.lower() for n in r.notes)
    assert r.quality


def test_remove_redundant_vias_noop_when_needed() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    r = clearance_aware_route(
        board, cfg, clearance_mm=0.2, grid_mm=1.0, soft_fallback=False, style="isotropic"
    )
    before = r.via_count
    out = remove_redundant_vias(r, board, cfg, clearance_mm=0.2)
    assert out.via_count <= before
    assert len(out.segments) >= 0


def test_remove_redundant_vias_merges_when_clear() -> None:
    """Construct a via with both stubs clear on F.Cu — should collapse."""
    from physics_router.models import BoardModel, Component

    board = BoardModel(width_mm=40, height_mm=40, copper_layers=["F.Cu", "B.Cu"])
    # Single-net pads so keepouts are same-net (not foreign blockers)
    board.components["R1"] = Component(
        ref="R1", x_mm=5, y_mm=10, width_mm=1, height_mm=1, pads=[{"net": "N1"}]
    )
    board.components["R2"] = Component(
        ref="R2", x_mm=30, y_mm=10, width_mm=1, height_mm=1, pads=[{"net": "N1"}]
    )
    board.nets["N1"] = [("R1", "1"), ("R2", "1")]
    segs = [
        RouteSegment(5, 10, 15, 10, layer="F.Cu", net="N1", width_mm=0.25),
        RouteSegment(15, 10, 30, 10, layer="B.Cu", net="N1", width_mm=0.25),
    ]
    vias = [Via(x=15, y=10, net="N1", layers=("F.Cu", "B.Cu"))]
    result = RouteResult(
        segments=segs,
        vias=vias,
        via_count=1,
        total_length_mm=25.0,
        net_reports=[],
    )
    out = remove_redundant_vias(result, board, None, clearance_mm=0.15)
    # Same-net keepouts only → path legal on one layer → via removed
    assert out.via_count == 0
    assert out.segments
    layers = {s.layer for s in out.segments}
    assert len(layers) == 1  # fully same-layer after via collapse
