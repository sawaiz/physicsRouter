"""Always-on router DRC: shorts, spacing, vias, outline escapes."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from physics_router.config_io import example_config
from physics_router.kicad_io import board_from_synthetic
from physics_router.models import BoardModel, Component
from physics_router.router import (
    CopperArea,
    RouteResult,
    RouteSegment,
    Via,
    attach_router_drc,
    clearance_aware_route,
    native_drc_check,
    _net_fully_connected,
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


def _fixed_pad_board() -> BoardModel:
    return BoardModel(
        width_mm=20,
        height_mm=20,
        copper_layers=["F.Cu", "In1.Cu", "B.Cu"],
        components={
            "U1": Component(
                ref="U1",
                x_mm=5.0,
                y_mm=5.0,
                rotation_deg=0.0,
                width_mm=2.0,
                height_mm=2.0,
                pads=[
                    {
                        "num": "1",
                        "net": "PAD_NET",
                        "x": 0.0,
                        "y": 0.0,
                        "rot": 0.0,
                        "w": 1.0,
                        "h": 1.0,
                        "shape": "rect",
                        "layers": ["F.Cu"],
                    }
                ],
            )
        },
        nets={"PAD_NET": [("U1", "1")]},
    )


def test_native_drc_detects_track_crossing_foreign_pad():
    board = _fixed_pad_board()
    route = RouteResult(
        segments=[RouteSegment(3.0, 5.0, 7.0, 5.0, "F.Cu", "OTHER", 0.2)]
    )
    rep = native_drc_check(route, clearance_mm=0.15, board=board)
    assert rep["pad_shorts"] == 1
    assert any(item.get("object_b") == "pad:U1" for item in rep["items"])


def test_native_drc_pad_is_net_and_layer_aware():
    board = _fixed_pad_board()
    route = RouteResult(
        segments=[
            # Owning-net copper may terminate on its pad.
            RouteSegment(3.0, 5.0, 7.0, 5.0, "F.Cu", "PAD_NET", 0.2),
            # Foreign inner copper does not collide with a front-only SMD pad.
            RouteSegment(3.0, 5.0, 7.0, 5.0, "In1.Cu", "OTHER", 0.2),
        ]
    )
    rep = native_drc_check(route, clearance_mm=0.15, board=board)
    assert rep["pad_shorts"] == 0
    assert rep["pad_spacing"] == 0


def test_native_drc_detects_through_via_on_foreign_front_pad():
    board = _fixed_pad_board()
    route = RouteResult(
        vias=[
            Via(
                5.0,
                5.0,
                net="OTHER",
                size_mm=0.6,
                layers=("F.Cu", "B.Cu"),
            )
        ],
        via_count=1,
    )
    rep = native_drc_check(route, clearance_mm=0.15, board=board)
    assert rep["pad_shorts"] == 1


def test_native_drc_forbids_same_net_via_in_pad():
    board = _fixed_pad_board()
    route = RouteResult(
        vias=[
            Via(
                5.0,
                5.0,
                net="PAD_NET",
                size_mm=0.6,
                layers=("F.Cu", "B.Cu"),
            )
        ],
        via_count=1,
    )

    rep = native_drc_check(route, clearance_mm=0.15, board=board)

    assert rep["pad_shorts"] == 1
    assert any(
        item["net_a"] == item["net_b"] == "PAD_NET"
        and item.get("object_b") == "pad:U1"
        for item in rep["items"]
    )


def test_same_net_via_near_pad_needs_no_electrical_clearance():
    board = _fixed_pad_board()
    route = RouteResult(
        vias=[
            Via(
                5.85,
                5.0,
                net="PAD_NET",
                size_mm=0.6,
                layers=("F.Cu", "B.Cu"),
            )
        ],
        via_count=1,
    )

    rep = native_drc_check(route, clearance_mm=0.15, board=board)

    assert rep["pad_shorts"] == 0
    assert rep["pad_spacing"] == 0


@pytest.mark.skipif(
    not Path("third_party/halo-90/pcb/halo-90.kicad_pcb").exists(),
    reason="HALO-90 PCB not cloned",
)
def test_halo_custom_battery_arc_blocks_through_via():
    """Custom-pad primitives extend far beyond the pad anchor's base size."""
    from physics_router.kicad_io import load_board_from_kicad_pcb

    board = load_board_from_kicad_pcb("third_party/halo-90/pcb/halo-90.kicad_pcb")
    route = RouteResult(
        vias=[
            Via(
                11.4,
                1.5,
                net="CPX-2",
                size_mm=0.6,
                drill_mm=0.3,
                layers=("F.Cu", "B.Cu"),
            )
        ],
        via_count=1,
    )
    rep = native_drc_check(route, clearance_mm=0.15, board=board)
    assert rep["pad_shorts"] >= 1
    assert any(item["net_b"] == "+3V" for item in rep["items"])


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
    out = purge_shorting_copper(
        r,
        board=BoardModel(
            width_mm=20,
            height_mm=20,
            copper_layers=["F.Cu"],
            components={},
            nets={"HIGH": [], "LOW": []},
        ),
        clearance_mm=0.2,
    )
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
    route = RouteResult(segments=[RouteSegment(14, -2, 16, 2, "F.Cu", "ESC", 0.25)])
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


def test_sequential_zero_violation_no_shorts_python():
    """Legal route commits only clean copper; shorts stay 0 end-to-end."""
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
    assert any("zero-violation" in n or "full-net commit" in n for n in r.notes)
    assert r.clearance_violations == 0
    rep = native_drc_check(r, clearance_mm=0.2, board=board)
    assert rep["shorts"] == 0
    assert rep["violations"] == 0
    # soft_fallback must never appear on legal path
    for nr in r.net_reports:
        assert "straight_fallback" not in (nr.method or "")


def test_sequential_zero_violation_native():
    """Native sequential path: one net at a time, zero DRC violations."""
    cfg = example_config()
    board = board_from_synthetic(cfg)
    r = clearance_aware_route(
        board,
        cfg,
        clearance_mm=0.2,
        grid_mm=0.5,
        soft_fallback=False,
        prefer_native=True,
        allow_vias=True,
    )
    assert any(
        "sequential zero-violation" in n or "full-net commit" in n for n in r.notes
    )
    assert r.clearance_violations == 0
    rep = native_drc_check(r, clearance_mm=0.2, board=board)
    assert rep["shorts"] == 0
    assert rep["violations"] == 0


def test_soft_fallback_forced_off_for_legal_route():
    """Passing soft_fallback=True must not create illegal copper when not guide."""
    cfg = example_config()
    board = board_from_synthetic(cfg)
    r = clearance_aware_route(
        board,
        cfg,
        clearance_mm=0.2,
        grid_mm=0.5,
        soft_fallback=True,  # ignored for legal clearance routes
        prefer_native=False,
        allow_vias=True,
    )
    rep = native_drc_check(r, clearance_mm=0.2, board=board)
    assert rep["shorts"] == 0
    for nr in r.net_reports:
        assert "straight_fallback" not in (nr.method or "")


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


def test_connectivity_requires_via_between_layers():
    board = BoardModel(
        width_mm=20,
        height_mm=10,
        copper_layers=["F.Cu", "B.Cu"],
        components={
            "A": Component(
                ref="A",
                x_mm=0,
                y_mm=0,
                pads=[{"num": "1", "net": "N", "layers": ["F.Cu"]}],
            ),
            "B": Component(
                ref="B",
                x_mm=10,
                y_mm=0,
                pads=[{"num": "1", "net": "N", "layers": ["B.Cu"]}],
            ),
        },
        nets={"N": [("A", "1"), ("B", "1")]},
    )
    segments = [
        RouteSegment(0, 0, 5, 0, "F.Cu", "N", 0.25),
        RouteSegment(5, 0, 10, 0, "B.Cu", "N", 0.25),
    ]
    assert not _net_fully_connected(board, "N", segments, [])
    assert _net_fully_connected(board, "N", segments, [Via(5, 0, net="N")])


def test_connectivity_rejects_inner_copper_at_front_smd_anchor():
    board = BoardModel(
        width_mm=20,
        height_mm=10,
        copper_layers=["F.Cu", "In1.Cu"],
        components={
            ref: Component(
                ref=ref,
                x_mm=x,
                y_mm=0,
                pads=[{"num": "1", "net": "N", "layers": ["F.Cu"]}],
            )
            for ref, x in (("A", 0), ("B", 10))
        },
        nets={"N": [("A", "1"), ("B", "1")]},
    )
    inner_only = [RouteSegment(0, 0, 10, 0, "In1.Cu", "N", 0.25)]
    assert not _net_fully_connected(board, "N", inner_only, [])


def test_copper_area_connects_contained_anchors():
    board = BoardModel(
        width_mm=20,
        height_mm=10,
        copper_layers=["B.Cu"],
        components={
            "A": Component(ref="A", x_mm=0, y_mm=0),
            "B": Component(ref="B", x_mm=10, y_mm=0),
        },
        nets={"GND": [("A", "1"), ("B", "1")]},
    )
    area = CopperArea(
        outline=[(-1, -2), (11, -2), (11, 2), (-1, 2)],
        layer="B.Cu",
        net="GND",
    )
    assert _net_fully_connected(board, "GND", [], [], areas=[area])
