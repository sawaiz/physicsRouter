"""Tests for KiCad DRC / render helpers (skip if KiCad not installed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.kicad_tools import find_kicad_cli, find_kicad_python, run_drc

ROOT = Path(__file__).resolve().parents[1]
PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"

pytestmark = pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")


def test_find_tools() -> None:
    assert find_kicad_cli() is not None
    # pcbnew optional but expected on full KiCad installs
    _ = find_kicad_python()


@pytest.mark.skipif(not PCB.exists(), reason="halo-90 not cloned")
def test_drc_runs(tmp_path: Path) -> None:
    report = run_drc(PCB, tmp_path / "drc.json")
    assert report.raw_path and Path(report.raw_path).exists()
    assert report.kicad_version
    d = report.to_dict()
    assert "error_count" in d
    assert "copper_violation_count" in d
    assert "by_type" in d
