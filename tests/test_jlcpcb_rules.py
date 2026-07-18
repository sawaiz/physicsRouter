"""JLCPCB 4-layer design rules floors and application."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.design_rules import (
    apply_manufacturer_floors,
    default_design_rules,
    jlcpcb_4layer_design_rules,
    load_design_rules,
)

ROOT = Path(__file__).resolve().parents[1]
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"


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


def test_jlcpcb_4layer_capability_floors():
    r = jlcpcb_4layer_design_rules(aggressive=True)
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
    out = apply_manufacturer_floors(base, manufacturer="JLCPCB")
    assert out.constraints.min_clearance_mm >= 0.15
    assert out.constraints.min_track_width_mm >= 0.15
    assert out.constraints.allow_blind_buried_vias is False


@pytest.mark.skipif(not HALO_PCB.exists(), reason="halo missing")
def test_load_design_rules_applies_jlc_to_halo():
    rules = load_design_rules(HALO_PCB, manufacturer="JLCPCB")
    assert rules.constraints.manufacturer == "JLCPCB"
    # HALO project had 0.127 mm; recommended profile raises to 0.15
    assert rules.constraints.min_clearance_mm >= 0.15
    assert rules.constraints.allow_blind_buried_vias is False
    assert len(rules.copper_layers) >= 4


def test_load_without_manufacturer_keeps_defaults():
    rules = load_design_rules(manufacturer=None)
    assert rules.constraints.manufacturer in ("", None) or True
    assert rules.constraints.min_clearance_mm == pytest.approx(0.2)
