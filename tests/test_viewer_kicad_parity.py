"""Verify 2D view landmarks and footprint transforms match KiCad / pcbnew."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from physics_router.config_io import load_config
from physics_router.kicad_io import load_board_from_kicad_pcb, local_to_board
from physics_router.viewer_export import board_to_viewer_dict

ROOT = Path(__file__).resolve().parents[1]
PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
CFG = ROOT / "examples/halo-90/placement_config.yaml"
PAD_FIXTURE = ROOT / "tests/fixtures/halo90_pcbnew_pads.json"

pytestmark = pytest.mark.skipif(not PCB.exists(), reason="HALO-90 PCB not cloned")


def _view_xy(x: float, y: float) -> tuple[float, float]:
    """Match viewer VIEW_FLIP_Y: (x, -y)."""
    return (x, -y)


def _load_viewer_board():
    cfg = load_config(CFG)
    board = load_board_from_kicad_pcb(PCB, cfg)
    bd = board_to_viewer_dict(board, cfg)
    by_ref = {c["ref"]: c for c in bd["components"]}
    return cfg, board, bd, by_ref


def _pad_board(comp: dict, num: str) -> tuple[float, float]:
    g = next(
        g
        for g in (comp.get("graphics") or [])
        if g.get("kind") == "pad" and str(g.get("num")) == str(num)
    )
    return local_to_board(comp["x"], comp["y"], comp["rot"], g["x"], g["y"])


def test_halo_view_landmarks_match_kicad_layout():
    """Hook top, switch left, LED ring angle step +4° (CW on screen)."""
    _, _, bd, by_ref = _load_viewer_board()
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
    _, board, _, _ = _load_viewer_board()
    d1 = board.components["D1"]
    u1 = board.components["U1"]
    assert d1.graphics, "D1 should have fp graphics from file"
    assert any(g.get("kind") == "pad" for g in d1.graphics)
    assert any(g.get("kind") == "line" for g in d1.graphics)
    assert u1.graphics and len(u1.graphics) > 10
    # Pad shape must be rect/circle, not the smd type atom
    pads = [g for g in d1.graphics if g.get("kind") == "pad"]
    assert all(
        g.get("shape") in ("rect", "circle", "roundrect", "oval", "trapezoid")
        for g in pads
    )


def test_edge_cuts_outline_visible():
    _, board, _, _ = _load_viewer_board()
    assert board.outline, "Edge.Cuts should be extracted"
    # Full disk circle synthesized for substrate fill (r≈12, not hook tip ~13.6)
    circles = [g for g in board.outline if g.get("kind") == "circle"]
    polys = [g for g in board.outline if g.get("kind") == "poly"]
    assert circles or polys
    if circles:
        assert any(abs(g["r"] - 12.0) < 0.5 for g in circles)


def test_edge_cuts_teardrop_arc_chain():
    """Classic gr_arc sampling must match pcbnew (hook tip, not floating wings)."""
    _, board, _, _ = _load_viewer_board()
    polys = [g for g in board.outline if g.get("kind") == "poly" and g.get("pts")]
    assert len(polys) >= 5
    # Collect endpoints; hook tip sits near board (0, -13.6) → view top after Y-flip
    ends = []
    for g in polys:
        ends.append(tuple(g["pts"][0]))
        ends.append(tuple(g["pts"][-1]))
    # Hook tip points (~±0.77, -13.63) must appear
    tip_pts = [
        p
        for p in ends
        if abs(p[1] + 13.63) < 0.05 and abs(abs(p[0]) - 0.77) < 0.05
    ]
    assert len(tip_pts) >= 2, f"hook tip endpoints missing: {ends}"
    # No spurious wing points outside r≈14 at y≈-11 (old bug: a0-sweep fillets)
    wings = [
        p for p in ends if abs(p[1] + 10.94) < 0.1 and abs(abs(p[0]) - 8.17) < 0.1
    ]
    assert not wings, f"stale wing endpoints from wrong arc sweep: {wings}"


def test_render_viewer_2d_script_landmarks():
    """Headless PNG renderer landmarks (same transform as control plane)."""
    from scripts.render_viewer_2d import landmark_report, render_board

    _, _, bd, _ = _load_viewer_board()
    rep = landmark_report(bd)
    assert rep["S1_left_of_U1"]
    assert rep["H1_above_U1"]
    assert rep["rot_step_cw_deg"] == pytest.approx(4.0)
    img = render_board(bd, size=400)
    assert img.size == (400, 400)


def test_local_to_board_matches_pcbnew_pad_positions():
    """Spot-check: footprint rot must use −angle or ±90° parts look 180° out."""
    truth = {
        "D1": {"1": (0.0, 10.475), "2": (0.0, 11.525)},
        "D46": {"1": (0.0, -10.475), "2": (0.0, -11.525)},
        "MK1": {
            "1": (-1.095, -3.925),
            "2": (1.095, -3.925),
            "3": (1.095, -5.275),
            "4": (-1.095, -5.275),
        },
        "S1": {"1": (-4.25, -1.625), "2": (-4.25, 1.625)},
        "U2": {"1": (-1.1375, 4.05)},
        "R1": {"1": (-0.8, 3.1), "2": (-1.8, 3.1)},
        "U1": {"1": (2.404163, -0.282843)},
    }
    _, _, _, by = _load_viewer_board()
    for ref, pads in truth.items():
        c = by[ref]
        for num, (tx, ty) in pads.items():
            bx, by_ = _pad_board(c, num)
            assert bx == pytest.approx(tx, abs=0.02), f"{ref} pad{num} x"
            assert by_ == pytest.approx(ty, abs=0.02), f"{ref} pad{num} y"


@pytest.mark.skipif(not PAD_FIXTURE.exists(), reason="pcbnew pad fixture missing")
def test_all_halo_pads_match_pcbnew_fixture():
    """Every footprint pad center vs pcbnew GetPosition (full board)."""
    fixture = json.loads(PAD_FIXTURE.read_text())
    _, _, _, by = _load_viewer_board()

    # Component origins + rotations
    for ref, t in fixture.items():
        assert ref in by, f"missing component {ref}"
        c = by[ref]
        assert c["x"] == pytest.approx(t["x"], abs=0.01), f"{ref} x"
        assert c["y"] == pytest.approx(t["y"], abs=0.01), f"{ref} y"
        drot = ((c["rot"] - t["rot"] + 180) % 360) - 180
        assert abs(drot) < 0.15, f"{ref} rot ours={c['rot']} fixture={t['rot']}"

    n_ok = 0
    for ref, t in fixture.items():
        c = by[ref]
        pads_g = {
            str(g.get("num")): g
            for g in (c.get("graphics") or [])
            if g.get("kind") == "pad"
        }
        for num, (tx, ty) in t["pads"].items():
            assert num in pads_g, f"missing pad graphics {ref}.{num}"
            g = pads_g[num]
            bx, by_ = local_to_board(c["x"], c["y"], c["rot"], g["x"], g["y"])
            err = math.hypot(bx - tx, by_ - ty)
            assert err < 0.05, (
                f"{ref} pad{num}: got=({bx:.4f},{by_:.4f}) "
                f"want=({tx},{ty}) err={err:.4f}"
            )
            n_ok += 1
    assert n_ok >= 200, f"expected full HALO pad set, got {n_ok}"


def test_view_space_pad_polarity_not_180_out():
    """After Y-flip, key pads must sit on the KiCad side (not mirrored 180°)."""
    _, _, _, by = _load_viewer_board()

    # D1 (bottom in view): pad1 toward board center (smaller |board y|)
    d1_1 = _pad_board(by["D1"], "1")
    d1_2 = _pad_board(by["D1"], "2")
    assert abs(d1_1[1]) < abs(d1_2[1]), "D1 pad1 must be inward (not 180° swapped)"

    # D46 (top in view, board y=-11): pad1 closer to origin
    d46_1 = _pad_board(by["D46"], "1")
    d46_2 = _pad_board(by["D46"], "2")
    assert abs(d46_1[1]) < abs(d46_2[1])

    # MK1: pad1 left of center in view (matches F.Fab notch / pin1)
    mk1_1 = _view_xy(*_pad_board(by["MK1"], "1"))
    assert mk1_1[0] < 0, f"MK1 pad1 should be left in view, got {mk1_1}"

    # S1: pad1 at board y=-1.625 → view y=+1.625 (above U1 after flip)
    s1_1 = _view_xy(*_pad_board(by["S1"], "1"))
    s1_2 = _view_xy(*_pad_board(by["S1"], "2"))
    assert s1_1[1] > s1_2[1], f"S1 pad1 above pad2 in view: {s1_1} vs {s1_2}"

    # U2 pin1 (rot 0): left and -Y of center → top-left of package in Y-flipped view
    u2_1 = _pad_board(by["U2"], "1")
    assert u2_1[0] < by["U2"]["x"]
    assert u2_1[1] < by["U2"]["y"], "U2 pad1 toward −Y board (top of package in view)"


def test_all_led_pad1_inward_on_ring():
    """All 90 LEDs: pad1 closer to origin than pad2 (radial polarity)."""
    _, _, _, by = _load_viewer_board()
    leds = [
        c
        for ref, c in by.items()
        if ref.startswith("D") and ref[1:].isdigit()
    ]
    assert len(leds) == 90
    for c in leds:
        p1 = _pad_board(c, "1")
        p2 = _pad_board(c, "2")
        r1 = math.hypot(*p1)
        r2 = math.hypot(*p2)
        assert r1 < r2 - 0.2, (
            f"{c['ref']}: pad1 r={r1:.3f} should be inward of pad2 r={r2:.3f}"
        )


def test_wrong_rotation_sign_would_fail_polarity():
    """Guard: +rot (old bug) must NOT match pcbnew for D1/MK1."""
    _, _, _, by = _load_viewer_board()

    def bad_local_to_board(fx, fy, frot, lx, ly):
        th = math.radians(float(frot or 0))  # WRONG: +rot
        c, s = math.cos(th), math.sin(th)
        return fx + lx * c - ly * s, fy + lx * s + ly * c

    d1 = by["D1"]
    g1 = next(g for g in d1["graphics"] if g.get("kind") == "pad" and g.get("num") == "1")
    wrong = bad_local_to_board(d1["x"], d1["y"], d1["rot"], g1["x"], g1["y"])
    right = local_to_board(d1["x"], d1["y"], d1["rot"], g1["x"], g1["y"])
    assert math.hypot(wrong[0] - right[0], wrong[1] - right[1]) > 0.5
    # wrong lands on pad2 position
    assert wrong[1] == pytest.approx(11.525, abs=0.05)
    assert right[1] == pytest.approx(10.475, abs=0.05)


def test_local_to_board_unit_identity_and_negation():
    """Unit geometry of local_to_board helper."""
    # rot 0: identity
    assert local_to_board(1, 2, 0, 0.5, -0.25) == pytest.approx((1.5, 1.75))
    # rot 90 file → effective -90 CCW: (lx,ly) → (ly, -lx)
    assert local_to_board(0, 0, 90, 1, 0) == pytest.approx((0, -1))
    assert local_to_board(0, 0, 90, 0, 1) == pytest.approx((1, 0))
    # rot -90 file → effective +90 CCW: (lx,ly) → (-ly, lx)
    assert local_to_board(0, 0, -90, 1, 0) == pytest.approx((0, 1))
    assert local_to_board(0, 0, -90, 0, 1) == pytest.approx((-1, 0))
