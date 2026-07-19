"""Official KiCad DRC oracle on exported router copper."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.kicad_tools import find_kicad_cli, kicad_drc_route
from physics_router.router import (
    RouteResult,
    RouteSegment,
    append_routes_to_kicad_pcb,
    parse_kicad_net_map,
)

ROOT = Path(__file__).resolve().parents[1]
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"


def test_parse_kicad_net_map_halo():
    if not HALO_PCB.exists():
        pytest.skip("halo pcb missing")
    text = HALO_PCB.read_text(encoding="utf-8", errors="replace")
    m = parse_kicad_net_map(text)
    assert m.get("GND") == 2
    assert m.get("+3V") == 1
    assert m.get("CPX-0") == 3
    assert m.get("CPX-1") == 12


def test_append_routes_uses_real_net_codes(tmp_path: Path):
    if not HALO_PCB.exists():
        pytest.skip("halo pcb missing")
    r = RouteResult(
        segments=[
            RouteSegment(0, 0, 5, 0, "F.Cu", "CPX-0", 0.25),
            RouteSegment(0, 0.1, 5, 0.1, "F.Cu", "CPX-1", 0.25),
        ]
    )
    dest = tmp_path / "out.kicad_pcb"
    append_routes_to_kicad_pcb(str(HALO_PCB), str(dest), r, clear_existing_copper=True)
    text = dest.read_text(encoding="utf-8")
    assert "(net 3)" in text  # CPX-0
    assert "(net 12)" in text  # CPX-1
    import re

    # Only board-level segments we appended (after footprints)
    segs = [ln for ln in text.splitlines() if ln.strip().startswith("(segment")]
    assert len(segs) >= 2
    for ln in segs:
        m = re.search(r"\(net (\d+)\)", ln)
        assert m is not None, ln
        assert int(m.group(1)) in (3, 12), ln


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
@pytest.mark.skipif(not HALO_PCB.exists(), reason="halo pcb missing")
def test_kicad_drc_detects_crossing_shorts(tmp_path: Path):
    """Crossing foreign nets must fail real KiCad DRC (not native-only)."""
    r = RouteResult(
        segments=[
            RouteSegment(-8, 0, 8, 0, "F.Cu", "CPX-0", 0.35),
            RouteSegment(0, -8, 0, 8, "F.Cu", "CPX-1", 0.35),
        ]
    )
    out = kicad_drc_route(HALO_PCB, r, work_dir=tmp_path / "drc", keep_files=True)
    assert out["available"] is True
    assert (tmp_path / "drc" / "routed.kicad_pro").exists()
    # Should report shorts / clearance / track errors
    assert out["copper_violation_count"] + out["error_count"] >= 1
    assert out["copper_passed"] is False or out["passed"] is False


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
@pytest.mark.skipif(not HALO_PCB.exists(), reason="halo pcb missing")
def test_kicad_route_drc_excludes_donor_board_copper_findings(tmp_path: Path):
    """Fixed pad/edge findings must not be charged to an empty candidate."""
    out = kicad_drc_route(
        HALO_PCB,
        RouteResult(),
        work_dir=tmp_path / "drc-empty",
        keep_files=True,
    )

    assert out["available"] is True
    assert out["copper_passed"] is True
    assert out["copper_violation_count"] == 0
    assert out["by_type"].get("copper_edge_clearance", 0) >= 1
