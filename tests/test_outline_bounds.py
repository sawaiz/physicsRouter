"""Edge.Cuts outline bounds: routes must stay inside the PCB silhouette."""

from __future__ import annotations

import math

import pytest

from physics_router.models import BoardModel, Component
from physics_router.router import (
    ObstacleMap,
    build_obstacle_map,
    clearance_aware_route,
    free_angle_route,
    outline_polygon_from_board,
    point_in_polygon,
)


def _circle_board(r: float = 12.0, n: int = 48) -> BoardModel:
    """Synthetic circular Edge.Cuts board with two pins inside."""
    pts = [
        (r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]
    return BoardModel(
        width_mm=2 * r + 4,
        height_mm=2 * r + 4,
        copper_layers=["F.Cu", "B.Cu"],
        components={
            "A": Component(
                ref="A",
                x_mm=-6.0,
                y_mm=0.0,
                width_mm=1.0,
                height_mm=1.0,
                pads=[{"num": "1", "net": "SIG", "x": 0.0, "y": 0.0}],
            ),
            "B": Component(
                ref="B",
                x_mm=6.0,
                y_mm=0.0,
                width_mm=1.0,
                height_mm=1.0,
                pads=[{"num": "1", "net": "SIG", "x": 0.0, "y": 0.0}],
            ),
        },
        nets={"SIG": [("A", "1"), ("B", "1")]},
        outline=[
            {
                "kind": "circle",
                "layer": "Edge.Cuts",
                "cx": 0.0,
                "cy": 0.0,
                "r": r,
                "width": 0.15,
            },
            {
                "kind": "poly",
                "layer": "Edge.Cuts",
                "pts": [[p[0], p[1]] for p in pts],
                "closed": True,
                "width": 0.15,
            },
        ],
    )


def test_point_in_polygon_unit():
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert point_in_polygon(5.0, 5.0, square)
    assert not point_in_polygon(15.0, 5.0, square)


def test_outline_polygon_from_circle_board():
    board = _circle_board(12.0)
    poly = outline_polygon_from_board(board)
    assert poly is not None and len(poly) >= 16
    assert point_in_polygon(0.0, 0.0, poly)
    assert not point_in_polygon(0.0, 13.0, poly)


def test_obstacle_map_rejects_outside_outline():
    board = _circle_board(12.0)
    om = build_obstacle_map(board, clearance_mm=0.2, layers=["F.Cu"])
    assert om.outline is not None
    assert om.in_bounds(0.0, 0.0)
    assert not om.in_bounds(0.0, 13.5)
    # Chord outside circle through exterior
    assert om.segment_blocked(-11.0, 8.0, 11.0, 8.0, "F.Cu", "SIG", width_mm=0.2)
    # Interior horizontal OK
    assert not om.segment_blocked(-5.0, 0.0, 5.0, 0.0, "F.Cu", "SIG", width_mm=0.2)


def test_free_angle_stays_inside_circular_outline():
    """Regression: route on circular board never leaves the outline."""
    r = 12.0
    board = _circle_board(r)
    om = build_obstacle_map(board, clearance_mm=0.15, layers=["F.Cu"])
    # Force a detour with a keepout that still leaves an in-outline corridor
    om.add_rect(0.0, 0.0, 2.0, 4.0, "F.Cu", net="BLOCK", inflate=True)
    start, goal = (-6.0, 0.0), (6.0, 0.0)
    path = free_angle_route(
        start, goal, "F.Cu", "SIG", om, grid_mm=0.5, width_mm=0.2, max_expansions=20000
    )
    assert path is not None and len(path) >= 2
    poly = om.outline
    assert poly is not None
    for p in path:
        assert point_in_polygon(p[0], p[1], poly), f"endpoint outside outline: {p}"
        assert math.hypot(p[0], p[1]) <= r + 0.05, f"radius {math.hypot(*p):.3f} > {r}"
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        mid = (0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1]))
        assert point_in_polygon(mid[0], mid[1], poly), f"midpoint outside: {mid}"
        assert not om.segment_blocked(
            a[0], a[1], b[0], b[1], "F.Cu", "SIG", width_mm=0.2
        )


def test_clearance_route_endpoints_inside_outline():
    """Full clearance-aware route on circular board: all segs stay inside."""
    r = 12.0
    board = _circle_board(r)
    result = clearance_aware_route(
        board,
        None,
        layers=["F.Cu", "B.Cu"],
        clearance_mm=0.15,
        grid_mm=0.5,
        soft_fallback=False,
        prefer_native=False,
        allow_vias=True,
    )
    assert result.segments, "expected at least one routed segment"
    poly = outline_polygon_from_board(board)
    assert poly is not None
    outside = 0
    checked = 0
    for s in result.segments:
        for pt in (
            (s.x1, s.y1),
            (s.x2, s.y2),
            (0.5 * (s.x1 + s.x2), 0.5 * (s.y1 + s.y2)),
        ):
            checked += 1
            if not point_in_polygon(pt[0], pt[1], poly):
                outside += 1
            if math.hypot(pt[0], pt[1]) > r + 0.15:
                outside += 1
    assert outside == 0, f"{outside}/{checked} sample points outside r={r} outline"


@pytest.mark.skipif(
    not __import__("pathlib").Path("third_party/halo-90/pcb/halo-90.kicad_pcb").exists(),
    reason="HALO-90 PCB not cloned",
)
def test_halo_outline_polygon_contains_components():
    from pathlib import Path

    from physics_router.config_io import load_config
    from physics_router.kicad_io import load_board_from_kicad_pcb

    cfg = load_config(Path("examples/halo-90/placement_config.yaml"))
    board = load_board_from_kicad_pcb(
        Path("third_party/halo-90/pcb/halo-90.kicad_pcb"), cfg
    )
    poly = outline_polygon_from_board(board)
    assert poly is not None and len(poly) >= 16
    om = build_obstacle_map(board, clearance_mm=0.15)
    assert om.outline is not None
    # Component centers should be inside
    for ref in ("U1", "S1", "D1", "H1"):
        c = board.components[ref]
        assert om.in_bounds(c.x_mm, c.y_mm), f"{ref} outside outline bounds"
    # Far exterior rejected
    assert not om.in_bounds(20.0, 0.0)
    assert not om.in_bounds(0.0, 20.0)
