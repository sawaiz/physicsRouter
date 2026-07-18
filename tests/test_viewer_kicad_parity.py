"""Verify 2D view landmarks match KiCad PCB / kicad-cli plot orientation."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.config_io import load_config
from physics_router.kicad_io import load_board_from_kicad_pcb
from physics_router.viewer_export import board_to_viewer_dict

ROOT = Path(__file__).resolve().parents[1]
PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
CFG = ROOT / "examples/halo-90/placement_config.yaml"

pytestmark = pytest.mark.skipif(not PCB.exists(), reason="HALO-90 PCB not cloned")


def _view_xy(x: float, y: float) -> tuple[float, float]:
    """Match viewer VIEW_FLIP_Y: (x, -y)."""
    return (x, -y)


def test_halo_view_landmarks_match_kicad_layout():
    """Hook top, switch left, LED ring angle step +4° (CW on screen)."""
    cfg = load_config(CFG)
    board = load_board_from_kicad_pcb(PCB, cfg)
    bd = board_to_viewer_dict(board, cfg)

    by_ref = {c["ref"]: c for c in bd["components"]}
    s1, h1, u1 = by_ref["S1"], by_ref["H1"], by_ref["U1"]
    d45, d46 = by_ref["D45"], by_ref["D46"]

    # File truth
    assert s1["x"] < 0 and abs(s1["x"] + 4.25) < 0.01
    assert h1["y"] < 0 and abs(h1["y"] + 13) < 0.01
    assert abs(s1["rot"] + 90) < 0.5  # PCB rot -90, not YAML 0

    # View space (Y-flip): S1 left, H1 above origin, D1 opposite hook
    sx, sy = _view_xy(s1["x"], s1["y"])
    hx, hy = _view_xy(h1["x"], h1["y"])
    ux, uy = _view_xy(u1["x"], u1["y"])
    d1 = by_ref["D1"]
    d1x, d1y = _view_xy(d1["x"], d1["y"])

    assert sx < ux, "S1 must stay left of U1 after view transform"
    assert hy > uy, "H1 (hook) must be above U1 after Y-flip (top of view)"
    assert d1y < uy, "D1 at +Y board is opposite hook after flip"

    # LED ring: +4° from D45 to D46 is clockwise progression in board angles
    assert d46["rot"] - d45["rot"] == pytest.approx(4.0, abs=0.1)


def test_footprint_graphics_loaded_from_pcb():
    cfg = load_config(CFG)
    board = load_board_from_kicad_pcb(PCB, cfg)
    d1 = board.components["D1"]
    u1 = board.components["U1"]
    assert d1.graphics, "D1 should have fp graphics from file"
    assert any(g.get("kind") == "pad" for g in d1.graphics)
    assert any(g.get("kind") == "line" for g in d1.graphics)
    assert u1.graphics and len(u1.graphics) > 10
    # Pad shape must be rect/circle, not the smd type atom
    pads = [g for g in d1.graphics if g.get("kind") == "pad"]
    assert all(g.get("shape") in ("rect", "circle", "roundrect", "oval", "trapezoid") for g in pads)


def test_edge_cuts_outline_visible():
    cfg = load_config(CFG)
    board = load_board_from_kicad_pcb(PCB, cfg)
    assert board.outline, "Edge.Cuts should be extracted"
    # Full disk circle synthesized for substrate fill (r≈12, not hook tip ~13.6)
    circles = [g for g in board.outline if g.get("kind") == "circle"]
    polys = [g for g in board.outline if g.get("kind") == "poly"]
    assert circles or polys
    if circles:
        assert any(abs(g["r"] - 12.0) < 0.5 for g in circles)


def test_edge_cuts_teardrop_arc_chain():
    """Classic gr_arc sampling must match pcbnew (hook tip, not floating wings)."""
    cfg = load_config(CFG)
    board = load_board_from_kicad_pcb(PCB, cfg)
    polys = [g for g in board.outline if g.get("kind") == "poly" and g.get("pts")]
    assert len(polys) >= 5
    # Collect endpoints; hook tip sits near board (0, -13.6) → view top after Y-flip
    ends = []
    for g in polys:
        ends.append(tuple(g["pts"][0]))
        ends.append(tuple(g["pts"][-1]))
    # Hook tip points (~±0.77, -13.63) must appear
    tip_pts = [p for p in ends if abs(p[1] + 13.63) < 0.05 and abs(abs(p[0]) - 0.77) < 0.05]
    assert len(tip_pts) >= 2, f"hook tip endpoints missing: {ends}"
    # No spurious wing points outside r≈14 at y≈-11 (old bug: a0-sweep fillets)
    wings = [p for p in ends if abs(p[1] + 10.94) < 0.1 and abs(abs(p[0]) - 8.17) < 0.1]
    assert not wings, f"stale wing endpoints from wrong arc sweep: {wings}"


def test_render_viewer_2d_script_landmarks():
    """Headless PNG renderer landmarks (same transform as control plane)."""
    from scripts.render_viewer_2d import landmark_report, render_board

    cfg = load_config(CFG)
    board = load_board_from_kicad_pcb(PCB, cfg)
    bd = board_to_viewer_dict(board, cfg)
    rep = landmark_report(bd)
    assert rep["S1_left_of_U1"]
    assert rep["H1_above_U1"]
    assert rep["rot_step_cw_deg"] == pytest.approx(4.0)
    img = render_board(bd, size=400)
    assert img.size == (400, 400)
