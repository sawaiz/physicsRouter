"""Tests for clearance router, OpenEMS export, and net import."""

from __future__ import annotations

from pathlib import Path

from physics_router.config_io import example_config
from physics_router.kicad_io import board_from_synthetic
from physics_router.net_import import build_net_labels, classify_net, extract_pcb_netclasses
from physics_router.openems_export import export_openems_bundle, parse_gerber
from physics_router.router import clearance_aware_route, topological_guide_route
from physics_router.models import NetClass


MINIMAL_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "GND")
  (net 2 "+5V")
  (net 3 "SW")
  (net_class "Power" "Power nets"
    (clearance 0.2)
    (trace_width 0.5)
    (add_net "+5V")
    (add_net "SW")
  )
  (net_class "Default" ""
    (clearance 0.15)
    (trace_width 0.25)
    (add_net "GND")
  )
  (footprint "R"
    (layer "F.Cu")
    (at 10 10 0)
    (property "Reference" "U1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "+5V"))
  )
)
"""

MINIMAL_SCH = """(kicad_sch
  (version 20231120)
  (generator "eeschema")
  (label "CLK_MCU"
    (at 50 50 0)
    (effects (font (size 1.27 1.27)))
  )
  (global_label "USB_DP"
    (shape input)
    (at 10 10 0)
    (property "Intersheetrefs" "${INTERSHEET_REFS}" (at 10 10 0))
  )
  (text "SW: keep loop tight for EMI"
    (at 0 0 0)
    (effects (font (size 1.27 1.27)))
  )
)
"""

SAMPLE_GERBER = """G04 demo gerber*
%MOMM*%
%FSLAX36Y36*%
%ADD10C,0.200000*%
%ADD11R,0.500000X0.500000*%
G01*
D10*
X0Y0D02*
X10000000Y0D01*
X10000000Y5000000D01*
D11*
X5000000Y2500000D03*
M02*
"""


def test_clearance_route_synthetic() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    routes = clearance_aware_route(board, cfg, clearance_mm=0.2, grid_mm=1.0, allow_vias=True)
    assert routes.total_length_mm > 0
    assert len(routes.segments) > 0
    d = routes.to_dict()
    assert "segments" in d and "vias" in d


def test_guide_route_still_works() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    g = topological_guide_route(board, cfg)
    assert g.total_length_mm > 0


def test_classify_net() -> None:
    assert classify_net("GND")[0] == NetClass.GROUND
    assert classify_net("+3V3")[0] == NetClass.POWER
    assert classify_net("USB_DP")[0] == NetClass.DIFFERENTIAL
    assert classify_net("foo", "Power")[0] == NetClass.POWER


def test_extract_pcb_netclasses(tmp_path: Path) -> None:
    pcb = tmp_path / "t.kicad_pcb"
    pcb.write_text(MINIMAL_PCB, encoding="utf-8")
    meta = extract_pcb_netclasses(pcb)
    assert "+5V" in meta
    assert meta["+5V"]["kicad_netclass"] == "Power"
    labels = build_net_labels(pcb_path=pcb)
    names = {n.name for n in labels}
    assert "SW" in names
    sw = next(n for n in labels if n.name == "SW")
    assert sw.weight >= 4.0


def test_schematic_import(tmp_path: Path) -> None:
    sch = tmp_path / "t.kicad_sch"
    sch.write_text(MINIMAL_SCH, encoding="utf-8")
    labels = build_net_labels(schematic_path=sch)
    names = {n.name for n in labels}
    assert "CLK_MCU" in names
    assert "USB_DP" in names
    # text note SW:
    assert "SW" in names
    sw = next(n for n in labels if n.name == "SW")
    assert "loop" in sw.notes.lower() or "emi" in sw.notes.lower()


def test_openems_export(tmp_path: Path) -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    routes = clearance_aware_route(board, cfg, clearance_mm=0.25, grid_mm=1.0)
    out = tmp_path / "ems"
    paths = export_openems_bundle(out, board=board, routes=routes, config=cfg)
    assert paths["geometry"].exists()
    assert paths["script"].exists()
    geom = paths["geometry"].read_text(encoding="utf-8")
    assert "primitives" in geom
    assert "stackup" in geom


def test_gerber_parse(tmp_path: Path) -> None:
    gbr = tmp_path / "f.gbr"
    gbr.write_text(SAMPLE_GERBER, encoding="utf-8")
    g = parse_gerber(gbr, layer_hint="F.Cu")
    assert len(g.polylines) >= 1 or len(g.flashes) >= 1
    paths = export_openems_bundle(tmp_path / "g", gerber_paths={"F.Cu": gbr})
    assert paths["geometry"].exists()
