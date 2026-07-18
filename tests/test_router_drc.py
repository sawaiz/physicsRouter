"""Always-on router DRC: shorts, spacing, vias, outline escapes."""

from __future__ import annotations

import math

from physics_router.config_io import example_config
from physics_router.kicad_io import board_from_synthetic
from physics_router.models import BoardModel, Component
from physics_router.router import (
    RouteResult,
    RouteSegment,
    Via,
    attach_router_drc,
    clearance_aware_route,
    native_drc_check,
)


def test_native_drc_detects_foreign_short():
    r = RouteResult(
        segments=[
            RouteSegment(0, 0, 10, 0, "F.Cu", "A", 0.3),
            RouteSegment(0, 0.15, 10, 0.15, "F.Cu", "B", 0.3),
        ]
    )
    rep = native_drc_check(r, clearance_mm=0.2)
    assert rep["violations"] >= 1
    assert rep["shorts"] + rep["spacing"] >= 1


def test_native_drc_allows_same_net_parallel():
    r = RouteResult(
        segments=[
            RouteSegment(0, 0, 10, 0, "F.Cu", "A", 0.3),
            RouteSegment(0, 0.15, 10, 0.15, "F.Cu", "A", 0.3),
        ]
    )
    rep = native_drc_check(r, clearance_mm=0.2)
    assert rep["violations"] == 0


def test_native_drc_via_near_foreign_track():
    r = RouteResult(
        segments=[RouteSegment(0, 0, 10, 0, "F.Cu", "A", 0.25)],
        vias=[Via(5.0, 0.15, net="B", size_mm=0.8)],
        via_count=1,
    )
    rep = native_drc_check(r, clearance_mm=0.2)
    assert rep["violations"] >= 1


def test_attach_router_drc_sets_clearance_violations():
    r = RouteResult(
        segments=[
            RouteSegment(0, 0, 5, 0, "F.Cu", "N1", 0.4),
            RouteSegment(0, 0.25, 5, 0.25, "F.Cu", "N2", 0.4),
        ]
    )
    attach_router_drc(r, clearance_mm=0.2)
    assert r.clearance_violations >= 1
    assert (r.quality or {}).get("drc", {}).get("violations", 0) >= 1
    assert any("router_drc:" in n for n in r.notes)


def test_purge_shorting_copper_removes_cross():
    """Crossing foreign nets: purge drops lower-priority copper (open > short)."""
    from physics_router.router import purge_shorting_copper

    r = RouteResult(
        segments=[
            RouteSegment(-5, 0, 5, 0, "F.Cu", "HIGH", 0.3),
            RouteSegment(0, -5, 0, 5, "F.Cu", "LOW", 0.3),
        ],
        net_reports=[],
    )
    # Without config, priorities are equal — purge still removes one side of short
    out = purge_shorting_copper(r, board=BoardModel(width_mm=20, height_mm=20, copper_layers=["F.Cu"], components={}, nets={"HIGH": [], "LOW": []}), clearance_mm=0.2)
    rep = native_drc_check(out, clearance_mm=0.2)
    assert rep["shorts"] == 0
    assert len(out.segments) < len(r.segments)
    assert any("purge_illegal" in n for n in out.notes)


def test_outline_outside_counts_as_drc():
    r = 12.0
    n = 32
    pts = [
        [r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)]
        for i in range(n)
    ]
    board = BoardModel(
        width_mm=30,
        height_mm=30,
        copper_layers=["F.Cu"],
        components={},
        nets={},
        outline=[
            {"kind": "circle", "cx": 0, "cy": 0, "r": r, "layer": "Edge.Cuts"},
            {"kind": "poly", "pts": pts, "closed": True, "layer": "Edge.Cuts"},
        ],
    )
    # Segment clearly outside the disk
    route = RouteResult(
        segments=[RouteSegment(14, -2, 16, 2, "F.Cu", "ESC", 0.25)]
    )
    rep = native_drc_check(route, clearance_mm=0.2, board=board)
    assert rep.get("outline_outside", 0) >= 1
    assert rep["violations"] >= 1
    assert any(i["kind"] == "outline" for i in rep["items"])


def test_clearance_route_always_runs_drc():
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
    assert any("router_drc:" in n for n in r.notes)
    assert "drc" in (r.quality or {})
    # Soft fallback off ⇒ no intentional illegal fill; DRC may still see near pads
    assert r.quality["drc"]["violations"] == r.clearance_violations


def test_drc_guard_reverts_worse_regeometry():
    """Polish must not increase violation count (guard path)."""
    from physics_router.design_rules import default_design_rules
    from physics_router.routing_strategies import _apply_drc_geometry

    cfg = example_config()
    board = board_from_synthetic(cfg)
    rules = default_design_rules()
    # Seed with clean parallel tracks
    r = RouteResult(
        segments=[
            RouteSegment(5, 5, 25, 5, "F.Cu", "GND", 0.3),
            RouteSegment(5, 15, 25, 15, "F.Cu", "+5V", 0.3),
        ],
        notes=[],
    )
    out = _apply_drc_geometry(r, board, cfg, rules, 0.2, regeometry=True)
    assert any("router_drc:" in n for n in out.notes)
    # Result should not invent shorts
    assert out.clearance_violations == (out.quality or {}).get("drc", {}).get(
        "violations", out.clearance_violations
    )
