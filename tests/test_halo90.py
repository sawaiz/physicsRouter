"""HALO-90 integration tests — skip if third_party/halo-90 is not cloned."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.config_io import load_config
from physics_router.kicad_io import load_board_from_kicad_pcb
from physics_router.models import NetClass
from physics_router.physics import geometric_score

ROOT = Path(__file__).resolve().parents[1]
PCB = ROOT / "third_party" / "halo-90" / "pcb" / "halo-90.kicad_pcb"
CFG = ROOT / "examples" / "halo-90" / "placement_config.yaml"

pytestmark = pytest.mark.skipif(not PCB.exists(), reason="Clone halo-90 into third_party/halo-90")


def test_halo90_config_labels() -> None:
    cfg = load_config(CFG)
    assert cfg.project_name == "halo-90"
    by_name = cfg.net_by_name()
    assert by_name["+3V"].net_class == NetClass.POWER
    assert by_name["+3V"].weight >= 5.0
    assert by_name["GND"].power_loop_group == "coin_cell"
    assert by_name["CPX-0"].emi_sensitive
    assert by_name["CPX-0"].power_loop_group == "charlieplex"
    assert by_name["MIC"].net_class == NetClass.ANALOG
    assert by_name["SDA"].pair_with == "SCL"
    assert by_name["SCL"].pair_with == "SDA"
    assert "NRST" in by_name["Net-(R3-Pad2)"].notes or "reset" in by_name["Net-(R3-Pad2)"].notes.lower()
    assert any(f.ref == "U1" and f.locked for f in cfg.fixed)
    assert any(f.ref == "BT1" for f in cfg.fixed)
    assert cfg.weight_for_net("CPX-7") > cfg.weight_for_net("TX")


def test_halo90_board_loads_and_scores() -> None:
    cfg = load_config(CFG)
    board = load_board_from_kicad_pcb(PCB, cfg)
    assert len(board.components) >= 100  # 90 LEDs + MCU + passives + pads
    assert "+3V" in board.nets and "GND" in board.nets
    assert all(f"CPX-{i}" in board.nets for i in range(10))
    # Fixed parts applied
    assert board.components["U1"].locked
    # LED ring locked via lock_ref_prefixes: ["D"]
    assert cfg.lock_ref_prefixes and "D" in cfg.lock_ref_prefixes
    leds = [r for r in board.components if r.startswith("D")]
    assert len(leds) >= 90
    assert all(board.components[r].locked for r in leds)
    assert len(board.movable_refs()) == 0  # mechanicals + LEDs locked on released layout
    sb = geometric_score(board, cfg)
    assert sb.total > 0
    assert sb.weighted_wirelength > 0
