"""Clearance enforcement: no touching foreign copper during route search."""

from __future__ import annotations

from physics_router.config_io import example_config
from physics_router.kicad_io import board_from_synthetic
from physics_router.router import (
    ObstacleMap,
    PaintedSeg,
    _seg_seg_min_dist,
    audit_same_layer_clearance,
    clearance_aware_route,
    fanout_anchor,
)


def test_seg_seg_detects_crossing():
    # Crossing segments at origin
    d = _seg_seg_min_dist(-1, 0, 1, 0, 0, -1, 0, 1)
    assert d < 0.05


def test_segment_blocked_by_painted_copper():
    om = ObstacleMap(50, 40, layers=["F.Cu"], clearance_mm=0.2)
    om.paint_trace(0, 0, 20, 0, "F.Cu", 0.3, "NET_A")
    # Parallel track 0.3mm away: half-widths 0.15+0.15 + clearance 0.2 = 0.5 needed
    assert om.segment_blocked(0, 0.3, 20, 0.3, "F.Cu", "NET_B", width_mm=0.3)
    # Far parallel OK
    assert not om.segment_blocked(0, 3.0, 20, 3.0, "F.Cu", "NET_B", width_mm=0.3)
    # Same net may run adjacent
    assert not om.segment_blocked(0, 0.3, 20, 0.3, "F.Cu", "NET_A", width_mm=0.3)


def test_fanout_anchors_differ_per_net():
    from physics_router.models import BoardModel, Component

    board = BoardModel(
        width_mm=20,
        height_mm=20,
        components={
            "U1": Component(
                ref="U1",
                x_mm=0,
                y_mm=0,
                width_mm=4,
                height_mm=4,
                pads=[
                    {"num": "1", "net": "CPX-0"},
                    {"num": "2", "net": "CPX-1"},
                    {"num": "3", "net": "CPX-2"},
                ],
            )
        },
        nets={
            "CPX-0": [("U1", "1")],
            "CPX-1": [("U1", "2")],
            "CPX-2": [("U1", "3")],
        },
    )
    a0 = fanout_anchor(board, "U1", "CPX-0")
    a1 = fanout_anchor(board, "U1", "CPX-1")
    a2 = fanout_anchor(board, "U1", "CPX-2")
    assert a0 != a1 and a1 != a2
    # Not all at origin
    assert abs(a0[0]) + abs(a0[1]) > 0.5


def test_clearance_route_no_near_miss_audit():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    r = clearance_aware_route(
        board,
        cfg,
        clearance_mm=0.25,
        grid_mm=0.5,
        soft_fallback=False,
        prefer_native=False,
    )
    audit = audit_same_layer_clearance(r, clearance_mm=0.25)
    # With soft_fallback off + continuous checks, near-miss should be zero or very low
    assert audit["near_miss_pairs"] == 0, audit
    for nr in r.net_reports:
        assert "straight_fallback" not in (nr.method or "")
