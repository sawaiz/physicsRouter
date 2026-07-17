"""ERC/DRC helpers and schematic discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.kicad_tools import (
    find_kicad_cli,
    find_schematic_for_pcb,
    run_drc,
    run_erc,
    validate_copper_board,
)

ROOT = Path(__file__).resolve().parents[1]
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
HALO_SCH = ROOT / "third_party/halo-90/pcb/halo-90.kicad_sch"


def test_find_schematic_for_halo():
    if not HALO_PCB.exists():
        pytest.skip("halo missing")
    sch = find_schematic_for_pcb(HALO_PCB)
    assert sch is not None
    assert sch.suffix == ".kicad_sch"
    assert sch.exists()


def test_find_schematic_missing(tmp_path: Path):
    pcb = tmp_path / "x.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n", encoding="utf-8")
    assert find_schematic_for_pcb(pcb) is None


@pytest.mark.skipif(find_kicad_cli() is None, reason="no kicad-cli")
@pytest.mark.skipif(not HALO_PCB.exists(), reason="no halo pcb")
def test_drc_and_validate_copper(tmp_path: Path):
    report = run_drc(HALO_PCB, tmp_path / "drc.json", severity_all=True)
    d = report.to_dict()
    assert d["violation_count"] >= 0
    assert "error_count" in d
    summary = validate_copper_board(HALO_PCB, tmp_path / "val")
    assert "error_count" in summary
    assert "copper_violation_count" in summary


@pytest.mark.skipif(find_kicad_cli() is None, reason="no kicad-cli")
@pytest.mark.skipif(not HALO_SCH.exists(), reason="no halo sch")
def test_erc_runs(tmp_path: Path):
    summary = run_erc(HALO_SCH, tmp_path / "erc.json")
    assert "error_count" in summary
    assert "violation_count" in summary
    assert summary.get("source")
    # raw file written when CLI succeeds
    if summary.get("raw_path") and not summary.get("error"):
        assert Path(summary["raw_path"]).exists()
