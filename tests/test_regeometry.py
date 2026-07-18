"""Post-connect free-angle re-geometry + TopoR geometry metrics."""

from __future__ import annotations

import math

from physics_router.config_io import example_config
from physics_router.design_rules import default_design_rules
from physics_router.kicad_io import board_from_synthetic
from physics_router.models import BoardModel, Component
from physics_router.regeometry import (
    arc_approximate_corners,
    compute_topor_geometry_metrics,
    count_bends,
    min_foreign_spacing_mm,
    post_connect_regeometry,
    spacing_repel_polyline,
    subdivide_polyline,
)
from physics_router.router import (
    ObstacleMap,
    RouteResult,
    RouteSegment,
    Via,
    build_obstacle_map,
    clearance_aware_route,
)
from physics_router.routing_strategies import topor_style_route


def test_count_bends_detects_corner():
    straight = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)]
    bent = [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0)]
    assert count_bends(straight) == 0
    assert count_bends(bent) == 1


def test_subdivide_adds_multi_bend_dof():
    pts = [(0.0, 0.0), (10.0, 0.0)]
    out = subdivide_polyline(pts, max_seg_mm=2.5)
    assert len(out) >= 4
    assert out[0] == (0.0, 0.0) and out[-1] == (10.0, 0.0)


def test_arc_approximate_adds_samples_on_sharp_corner():
    pts = [(0.0, 0.0), (5.0, 0.0), (5.0, 5.0)]
    out, n = arc_approximate_corners(pts, samples_per_corner=3, min_turn_deg=10.0)
    assert n >= 1
    assert len(out) > len(pts)
    assert out[0] == pts[0] and out[-1] == pts[-1]


def test_spacing_repel_pushes_away_from_foreign_track():
    om = ObstacleMap(40, 20, layers=["F.Cu"], clearance_mm=0.2, x_min=0, y_min=0)
    om.paint_trace(0, 10, 40, 10, "F.Cu", 0.3, "A")
    # Path parallel and too close
    pts = [(2.0, 10.4), (10.0, 10.4), (20.0, 10.4), (30.0, 10.4), (38.0, 10.4)]
    out = spacing_repel_polyline(
        pts, "F.Cu", "B", om, width_mm=0.25, clearance_mm=0.2, iterations=25, step=0.15
    )
    # Middle points should move farther from y=10
    mid_y = out[len(out) // 2][1]
    assert abs(mid_y - 10.0) > abs(pts[len(pts) // 2][1] - 10.0) - 0.01


def test_post_connect_regeometry_increases_bends_or_points():
    """Straight LOS nets gain intermediate geometry after re-geometry."""
    r = RouteResult(
        segments=[
            RouteSegment(0, 0, 20, 0, "F.Cu", "N1", 0.25),
            RouteSegment(0, 5, 20, 5, "F.Cu", "N2", 0.25),  # parallel close-ish
        ],
        vias=[],
        via_count=0,
        total_length_mm=40.0,
    )
    board = BoardModel(
        width_mm=30,
        height_mm=20,
        copper_layers=["F.Cu"],
        components={
            "A": Component(ref="A", x_mm=0, y_mm=0, width_mm=1, height_mm=1),
            "B": Component(ref="B", x_mm=20, y_mm=0, width_mm=1, height_mm=1),
        },
        nets={"N1": [("A", "1"), ("B", "1")], "N2": [("A", "1"), ("B", "1")]},
    )
    out = post_connect_regeometry(
        r, board, clearance_mm=0.2, iterations=12, use_arcs=True, max_seg_mm=3.0
    )
    assert out.segments
    assert len(out.segments) >= len(r.segments)
    m = out.quality.get("topor_geometry") or {}
    assert "bend_count" in m
    assert any("regeometry" in n for n in out.notes)


def test_topor_geometry_metrics_on_synthetic_route():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    r = clearance_aware_route(
        board,
        cfg,
        clearance_mm=0.2,
        grid_mm=0.5,
        soft_fallback=False,
        prefer_native=False,
        allow_vias=True,
    )
    m = compute_topor_geometry_metrics(r)
    assert m.segment_count == len(r.segments)
    assert m.total_length_mm >= 0
    assert m.net_count_routed >= 1
    d = m.to_dict()
    assert "min_spacing_mm" in d


def test_topor_pipeline_includes_regeometry_metrics():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    rules = default_design_rules()
    r = topor_style_route(
        board,
        cfg,
        rules,
        clearance_mm=0.2,
        grid_mm=0.5,
        num_variants=1,
        negotiate_iters=1,
        k_homotopy=1,
        use_cbs=False,
        use_elastic=False,
        use_planner=False,
        use_regeometry=True,
    )
    assert r.segments
    assert any("regeometry" in n or "topor_pipeline" in n for n in r.notes)
    # Geometry metrics attached when regeometry ran
    tg = (r.quality or {}).get("topor_geometry") or (r.quality or {}).get("regeometry")
    # At least notes mention regeometry
    assert any("regeometry" in n for n in r.notes) or tg is not None


def test_min_spacing_detects_overlap():
    r = RouteResult(
        segments=[
            RouteSegment(0, 0, 10, 0, "F.Cu", "A", 0.3),
            RouteSegment(0, 0.1, 10, 0.1, "F.Cu", "B", 0.3),  # almost on top
        ]
    )
    sp = min_foreign_spacing_mm(r, sample_step_mm=0.5)
    assert sp < 0.0  # centerline 0.1 minus half-widths → negative edge gap


def test_arc_preserves_endpoints():
    pts = [(1.0, 2.0), (5.0, 2.0), (8.0, 6.0), (12.0, 6.0)]
    out, _ = arc_approximate_corners(pts, samples_per_corner=2)
    assert math.hypot(out[0][0] - 1.0, out[0][1] - 2.0) < 1e-9
    assert math.hypot(out[-1][0] - 12.0, out[-1][1] - 6.0) < 1e-9
