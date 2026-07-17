"""Tests for KiCad design rules / stackup and multilayer routing policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.config_io import example_config
from physics_router.design_rules import default_design_rules, load_design_rules
from physics_router.kicad_io import board_from_synthetic
from physics_router.routing_strategies import (
    multilayer_route,
    ordered_nets,
    pre_route_analysis,
)

ROOT = Path(__file__).resolve().parents[1]
HALO_PCB = ROOT / "third_party" / "halo-90" / "pcb" / "halo-90.kicad_pcb"
HALO_PRO = ROOT / "third_party" / "halo-90" / "pcb" / "halo-90.kicad_pro"


def test_default_rules() -> None:
    r = default_design_rules()
    assert "F.Cu" in r.copper_layers
    assert r.clearance_for_net("GND") > 0
    assert r.track_width_for_net("SIG") > 0


@pytest.mark.skipif(not HALO_PCB.exists(), reason="halo-90 not cloned")
def test_halo90_stackup_and_drc() -> None:
    rules = load_design_rules(pcb_path=HALO_PCB, pro_path=HALO_PRO if HALO_PRO.exists() else None)
    # 4-layer earring board
    assert len(rules.copper_layers) == 4
    assert rules.copper_layers[0] == "F.Cu"
    assert rules.copper_layers[-1] == "B.Cu"
    assert "In1.Cu" in rules.copper_layers
    assert rules.constraints.min_clearance_mm <= 0.2
    assert rules.constraints.min_track_width_mm <= 0.2
    assert rules.stackup  # from PCB setup
    copper_in_stack = [s for s in rules.stackup if s.layer_type == "copper"]
    assert len(copper_in_stack) >= 2
    assert rules.preferred_plane_layers  # inners for 4L
    # DRC floor enforced
    assert rules.clearance_for_net("CPX-0") >= rules.constraints.min_clearance_mm
    s = rules.summary()
    assert s["layer_count"] == 4


def test_pre_route_and_multilayer_synthetic() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    rules = default_design_rules()
    rules.copper_layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    from physics_router.design_rules import _finalize_layer_roles

    _finalize_layer_roles(rules)
    report = pre_route_analysis(board, cfg, rules)
    assert report.net_count >= 1
    assert report.suggestions
    order = ordered_nets(board, cfg)
    assert order[0] in board.nets
    # GND/power should sort early when labeled
    if "GND" in order and "+5V" in order:
        assert order.index("GND") < order.index("AIN0")
    routes = multilayer_route(board, cfg, rules, clearance_mm=0.2, grid_mm=1.0)
    assert routes.total_length_mm >= 0
    assert any("drc:" in n for n in routes.notes)
