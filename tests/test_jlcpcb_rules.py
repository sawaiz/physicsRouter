"""JLCPCB 2/4/6-layer design rules floors and profile catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.design_rules import (
    apply_manufacturer_floors,
    default_design_rules,
    jlcpcb_2layer_design_rules,
    jlcpcb_4layer_design_rules,
    jlcpcb_6layer_design_rules,
    jlcpcb_design_rules,
    list_jlcpcb_profiles,
    load_design_rules,
    parse_jlc_profile,
)

ROOT = Path(__file__).resolve().parents[1]
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"


def test_list_profiles_covers_2_4_6():
    ids = {p["id"] for p in list_jlcpcb_profiles()}
    for n in (2, 4, 6):
        assert f"{n}layer_recommended" in ids
        assert f"{n}layer_capability" in ids
    for p in list_jlcpcb_profiles():
        assert p["suggestions"]
        assert p["limitations"]


def test_parse_jlc_profile_aliases():
    assert parse_jlc_profile("2l") == (2, False)
    assert parse_jlc_profile("6layer_capability") == (6, True)
    assert parse_jlc_profile("4layer") == (4, False)


def test_jlcpcb_2layer_recommended():
    r = jlcpcb_2layer_design_rules()
    assert len(r.copper_layers) == 2
    assert r.constraints.manufacturer_profile == "2layer_recommended"
    assert r.constraints.min_clearance_mm >= 0.2
    assert r.constraints.allow_blind_buried_vias is False


def test_jlcpcb_4layer_recommended_floors():
    r = jlcpcb_4layer_design_rules(aggressive=False)
    c = r.constraints
    assert c.manufacturer == "JLCPCB"
    assert c.manufacturer_profile == "4layer_recommended"
    assert len(r.copper_layers) == 4
    assert c.min_clearance_mm >= 0.15
    assert c.min_track_width_mm >= 0.15
    assert c.min_via_drill_mm >= 0.3
    assert c.min_via_diameter_mm >= 0.6
    assert c.allow_blind_buried_vias is False
    assert c.allow_microvias is False
    assert c.board_thickness_mm == 1.6
    assert "Power" in r.net_classes
    assert r.net_classes["Power"].track_width_mm >= 0.4


def test_jlcpcb_6layer_recommended():
    r = jlcpcb_6layer_design_rules()
    assert len(r.copper_layers) == 6
    assert r.constraints.manufacturer_profile == "6layer_recommended"
    assert r.preferred_plane_layers
    assert r.constraints.allow_microvias is False


def test_jlcpcb_4layer_capability_floors():
    r = jlcpcb_design_rules(layers=4, aggressive=True)
    c = r.constraints
    assert c.min_clearance_mm == pytest.approx(0.09)
    assert c.min_track_width_mm == pytest.approx(0.09)
    assert c.min_via_drill_mm == pytest.approx(0.2)
    assert c.allow_blind_buried_vias is False


def test_apply_floors_raises_loose_rules():
    base = default_design_rules()
    base.constraints.min_clearance_mm = 0.05  # looser than JLC
    base.constraints.min_track_width_mm = 0.05
    base.constraints.allow_blind_buried_vias = True
    out = apply_manufacturer_floors(base, manufacturer="JLCPCB", profile="4layer_recommended")
    assert out.constraints.min_clearance_mm >= 0.15
    assert out.constraints.min_track_width_mm >= 0.15
    assert out.constraints.allow_blind_buried_vias is False


def test_apply_2layer_profile_on_default():
    base = default_design_rules()
    out = apply_manufacturer_floors(base, manufacturer="JLCPCB", profile="2layer_recommended")
    assert out.constraints.manufacturer_profile == "2layer_recommended"
    # default board is 2L already — still 2 copper
    assert len(out.copper_layers) == 2


@pytest.mark.skipif(not HALO_PCB.exists(), reason="halo missing")
def test_load_design_rules_applies_jlc_to_halo():
    rules = load_design_rules(HALO_PCB, manufacturer="JLCPCB", jlc_profile="4layer_recommended")
    assert rules.constraints.manufacturer == "JLCPCB"
    assert rules.constraints.min_clearance_mm >= 0.15
    assert rules.constraints.allow_blind_buried_vias is False
    assert len(rules.copper_layers) >= 4


def test_load_without_manufacturer_keeps_defaults():
    rules = load_design_rules(manufacturer=None)
    assert rules.constraints.min_clearance_mm == pytest.approx(0.2)
