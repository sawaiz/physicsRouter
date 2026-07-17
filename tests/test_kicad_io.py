"""KiCad PCB load/rewrite tests using a minimal synthetic file."""

from __future__ import annotations

from pathlib import Path

from physics_router.config_io import example_config
from physics_router.kicad_io import apply_placement_to_kicad_pcb, load_board_from_kicad_pcb

MINIMAL_PCB = """(kicad_pcb
  (version 20240108)
  (generator "physics_router_test")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "+5V")
  (net 2 "GND")
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 10 10 0)
    (property "Reference" "U1" (at 0 0) (layer "F.SilkS"))
    (property "Value" "Buck" (at 0 1) (layer "F.Fab"))
    (pad "1" smd rect (at -2 0) (size 1 1) (layers "F.Cu") (net 1 "+5V"))
    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 2 "GND"))
  )
  (footprint "Connector:USB"
    (layer "F.Cu")
    (at 2 20 0)
    (property "Reference" "J1" (at 0 0) (layer "F.SilkS"))
    (property "Value" "USB" (at 0 1) (layer "F.Fab"))
    (pad "GND" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "GND"))
  )
)
"""


def test_load_and_rewrite(tmp_path: Path) -> None:
    pcb = tmp_path / "t.kicad_pcb"
    pcb.write_text(MINIMAL_PCB, encoding="utf-8")
    cfg = example_config()
    board = load_board_from_kicad_pcb(pcb, cfg)
    assert "U1" in board.components
    assert "J1" in board.components
    assert "+5V" in board.nets or any("+5V" in n for n in board.nets)

    out = tmp_path / "placed.kicad_pcb"
    apply_placement_to_kicad_pcb(
        pcb,
        {"U1": (12.5, 14.0, 90.0), "J1": (2.0, 20.0, 0.0)},
        out,
    )
    text = out.read_text(encoding="utf-8")
    assert "12.5000" in text or "12.5" in text
    assert "U1" in text
