"""HALO ring / polar routing (halo.js style)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from physics_router.config_io import load_config
from physics_router.halo_ring import (
    arc_polyline,
    detect_led_ring,
    halo_ring_route,
    polar_path,
    track_radius,
)
from physics_router.kicad_io import load_board_from_kicad_pcb

ROOT = Path(__file__).resolve().parents[1]
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
HALO_CFG = ROOT / "examples/halo-90/placement_config.yaml"


def test_polar_path_stays_near_track_radius():
    pts = polar_path((0, 11), (11, 0), cx=0, cy=0, track_r=9.0, step_deg=5)
    assert len(pts) >= 4
    # mid arc samples near r=9
    mid = pts[len(pts) // 2]
    r = math.hypot(mid[0], mid[1])
    assert abs(r - 9.0) < 0.5


def test_arc_polyline_shortest_direction():
    pts = arc_polyline(10, 350, 10, cx=0, cy=0, step_deg=5)
    # Should take short way (~20°) not 340°
    assert len(pts) < 20


def test_track_radius_decreases_with_track():
    assert track_radius(11, 0) == 11
    assert track_radius(11, 1) < track_radius(11, 0)
    assert track_radius(11, 3) < track_radius(11, 1)


@pytest.mark.skipif(not HALO_PCB.exists() or not HALO_CFG.exists(), reason="halo missing")
def test_detect_halo_led_ring():
    cfg = load_config(HALO_CFG)
    board = load_board_from_kicad_pcb(HALO_PCB, cfg)
    ring = detect_led_ring(board)
    assert ring is not None
    assert len(ring.led_refs) >= 90
    assert 10.5 < ring.radius < 11.5


@pytest.mark.skipif(not HALO_PCB.exists() or not HALO_CFG.exists(), reason="halo missing")
def test_halo_ring_route_produces_concentric_copper():
    cfg = load_config(HALO_CFG)
    board = load_board_from_kicad_pcb(HALO_PCB, cfg)
    r = halo_ring_route(board, cfg, route_non_cpx=False)
    assert r.segments, "expected CPX polar copper"
    assert any("halo_ring" in n for n in r.notes)
    # Most length should sit near LED radius bands (not pure center chords only)
    ring = detect_led_ring(board)
    assert ring
    mid_r = []
    for s in r.segments[:80]:
        mx, my = 0.5 * (s.x1 + s.x2), 0.5 * (s.y1 + s.y2)
        mid_r.append(math.hypot(mx - ring.cx, my - ring.cy))
    assert mid_r
    # Median radius should be well above center (not star through origin)
    mid_r.sort()
    med = mid_r[len(mid_r) // 2]
    assert med > 5.0, f"routes look like center star, median r={med}"
    # Quality should be computed
    assert r.quality and "score" in r.quality
