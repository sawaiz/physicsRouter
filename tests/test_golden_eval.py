"""Golden copper extract + score vs human routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.compare import compare_to_golden
from physics_router.golden_eval import evaluate_board, load_manifest, run_suite
from physics_router.kicad_io import load_board_from_kicad_pcb
from physics_router.router import (
    RouteResult,
    RouteSegment,
    Via,
    extract_routes_from_kicad_pcb,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/golden/simple_2net.kicad_pcb"
HALO = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
MANIFEST = ROOT / "examples/golden/manifest.yaml"


def test_extract_simple_2net_human_copper():
    assert FIXTURE.is_file()
    board = load_board_from_kicad_pcb(FIXTURE)
    human = extract_routes_from_kicad_pcb(FIXTURE, board_nets=board.nets)

    assert len(human.segments) == 7
    assert human.via_count == 2
    nets = {s.net for s in human.segments}
    assert nets == {"SIG_A", "SIG_B"}
    assert human.total_length_mm > 0
    # both nets have copper → not unrouted
    assert "SIG_A" not in human.unrouted_nets
    assert "SIG_B" not in human.unrouted_nets

    sa = [s for s in human.segments if s.net == "SIG_A"]
    layers = {s.layer for s in sa}
    assert "F.Cu" in layers and "B.Cu" in layers
    assert all(v.net == "SIG_A" for v in human.vias)


def test_extract_roundtrip_append(tmp_path: Path):
    human = extract_routes_from_kicad_pcb(FIXTURE)
    dest = tmp_path / "rewritten.kicad_pcb"
    from physics_router.router import append_routes_to_kicad_pcb

    append_routes_to_kicad_pcb(
        str(FIXTURE), str(dest), human, clear_existing_copper=True
    )
    again = extract_routes_from_kicad_pcb(dest)
    assert len(again.segments) == len(human.segments)
    assert again.via_count == human.via_count
    assert abs(again.total_length_mm - human.total_length_mm) < 1e-3


def test_compare_to_golden_identity_and_partial():
    human = extract_routes_from_kicad_pcb(FIXTURE)
    full = compare_to_golden(human, human, hard_violations=0)
    assert full["kind"] == "golden"
    assert full["completion"]["ratio"] == 1.0
    assert full["policy"]["zero_hard_drc"] is True
    assert full["golden_score"] >= 90
    assert full["golden_grade"] in ("A", "B")

    # AR only routes SIG_B-ish copper: one short segment, no vias
    partial = RouteResult(
        segments=[
            RouteSegment(5.8, 10, 25.8, 10, "F.Cu", "SIG_B", 0.25),
        ],
        vias=[],
        via_count=0,
        total_length_mm=20.0,
        unrouted_nets=["SIG_A"],
    )
    cmp = compare_to_golden(partial, human, hard_violations=0)
    assert cmp["completion"]["ratio"] == 0.5
    assert "SIG_A" in cmp["completion"]["missing_nets"]
    assert cmp["golden_score"] < full["golden_score"]


def test_compare_penalizes_hard_drc():
    human = extract_routes_from_kicad_pcb(FIXTURE)
    dirty = compare_to_golden(human, human, hard_violations=5)
    clean = compare_to_golden(human, human, hard_violations=0)
    assert dirty["golden_score"] < clean["golden_score"]
    assert dirty["policy"]["zero_hard_drc"] is False


@pytest.mark.skipif(not HALO.is_file(), reason="halo pcb missing")
def test_extract_halo_human_scale():
    human = extract_routes_from_kicad_pcb(HALO)
    # Released HALO is fully routed; counts from docs (~4335 segs, 182 vias)
    assert len(human.segments) > 4000
    assert human.via_count > 150
    assert human.total_length_mm > 500
    nets = {s.net for s in human.segments if s.net}
    assert "GND" in nets or "CPX-0" in nets


def test_manifest_loads():
    m = load_manifest(MANIFEST)
    ids = [b["id"] for b in m["boards"]]
    assert "simple_2net" in ids
    assert "halo-90" in ids


def test_evaluate_extract_only(tmp_path: Path):
    row = evaluate_board(
        {
            "id": "simple_2net",
            "pcb": str(FIXTURE),
            "expect": "partial_ok",
            "_base": str(ROOT),
        },
        out_dir=tmp_path / "simple",
        extract_only=True,
    )
    assert not row.get("skipped")
    assert row["human"]["segments"] == 7
    assert row["human"]["vias"] == 2
    assert Path(row["human_json"]).is_file()


def test_run_suite_extract_only(tmp_path: Path):
    summary = run_suite(
        MANIFEST,
        out_dir=tmp_path / "suite",
        board_ids=["simple_2net"],
        extract_only=True,
    )
    assert summary["counts"]["total"] == 1
    assert summary["passed"] is True
    assert Path(summary["out_json"]).is_file()


def test_evaluate_missing_pcb(tmp_path: Path):
    row = evaluate_board(
        {"id": "nope", "pcb": "does/not/exist.kicad_pcb", "_base": str(tmp_path)},
        extract_only=True,
    )
    assert row["skipped"] is True
    assert row["passed"] is False
