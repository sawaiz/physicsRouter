"""Zone-aware goldens, pin-access sharing, rules profiles, CBS, deadlines."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.compare import compare_to_golden
from physics_router.design_rules import (
    apply_rules_profile,
    default_design_rules,
    load_design_rules,
)
from physics_router.golden_eval import (
    evaluate_board,
    pin_access_metrics,
    run_suite,
)
from physics_router.kicad_io import load_board_from_kicad_pcb
from physics_router.models import BoardModel, Component
from physics_router.pin_access import AccessSite, PadAccess, PinAccessPlan, build_pin_access_plan
from physics_router.router import (
    CopperArea,
    RouteResult,
    RouteSegment,
    Via,
    extract_copper_areas_from_kicad_pcb,
    extract_routes_from_kicad_pcb,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/golden/simple_2net.kicad_pcb"
CI_MANIFEST = ROOT / "examples/golden/ci_manifest.yaml"
ZONE_FIXTURE = ROOT / "tests/fixtures/golden/zone_power.kicad_pcb"


def _write_zone_fixture(path: Path) -> None:
    """Minimal PCB: two pads on GND + a filled zone (no tracks)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """(kicad_pcb (version 20240108) (generator "physics_router_test")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (footprint "R_0603" (layer "F.Cu") (at 5 5)
    (property "Reference" "R1" (at 0 -1.2 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -0.8 0) (size 0.5 0.6) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.8 0) (size 0.5 0.6) (layers "F.Cu") (net 2 "SIG"))
  )
  (footprint "R_0603" (layer "F.Cu") (at 20 5)
    (property "Reference" "R2" (at 0 -1.2 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -0.8 0) (size 0.5 0.6) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.8 0) (size 0.5 0.6) (layers "F.Cu") (net 2 "SIG"))
  )
  (gr_rect (start 0 0) (end 30 15) (layer "Edge.Cuts")
    (stroke (width 0.1) (type default)) (fill none))
  (segment (start 5.8 5) (end 19.2 5) (width 0.25) (layer "F.Cu") (net 2)
    (tstamp aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa))
  (zone (net 1) (net_name "GND") (layer "F.Cu") (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.25)
    (fill yes)
    (polygon (pts (xy 2 2) (xy 28 2) (xy 28 12) (xy 2 12)))
    (filled_polygon (layer "F.Cu")
      (pts (xy 2 2) (xy 28 2) (xy 28 12) (xy 2 12)))
  )
)
""",
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def zone_pcb(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # Prefer committed fixture if present; else write under tests/fixtures
    if ZONE_FIXTURE.is_file():
        return ZONE_FIXTURE
    p = ROOT / "tests/fixtures/golden/zone_power.kicad_pcb"
    _write_zone_fixture(p)
    return p


def test_extract_zones_as_copper_areas(zone_pcb: Path):
    areas = extract_copper_areas_from_kicad_pcb(zone_pcb)
    assert len(areas) >= 1
    assert any(a.net == "GND" for a in areas)

    human = extract_routes_from_kicad_pcb(zone_pcb)
    assert len(human.areas) >= 1
    # SIG has a track; GND has a zone pour
    nets = {s.net for s in human.segments} | {a.net for a in human.areas}
    assert "GND" in nets
    assert "SIG" in nets


def test_zone_only_net_counts_in_golden_completion(zone_pcb: Path):
    human = extract_routes_from_kicad_pcb(zone_pcb)
    # AR only routes SIG as track — missing GND pour unless it also has area
    ar = RouteResult(
        segments=[RouteSegment(5.8, 5, 19.2, 5, "F.Cu", "SIG", 0.25)],
        vias=[],
        areas=[],
        via_count=0,
        total_length_mm=13.4,
        unrouted_nets=["GND"],
    )
    cmp = compare_to_golden(ar, human, hard_violations=0)
    assert "GND" in (cmp["completion"].get("human_zone_only_nets") or []) or any(
        r.get("zone_only") for r in (cmp.get("per_net") or []) if r.get("net") == "GND"
    )
    assert "GND" in (cmp["completion"].get("missing_nets") or [])
    # When AR also has a GND area, completion should include GND
    ar2 = RouteResult(
        segments=list(ar.segments),
        areas=[
            CopperArea(
                outline=[(2, 2), (28, 2), (28, 12), (2, 12)],
                layer="F.Cu",
                net="GND",
            )
        ],
        via_count=0,
        total_length_mm=13.4,
        unrouted_nets=[],
    )
    cmp2 = compare_to_golden(ar2, human, hard_violations=0)
    assert cmp2["completion"]["ratio"] == 1.0


def test_shared_escape_resources_merge_nearby_sites():
    plan = PinAccessPlan(
        by_net={
            "N1": [
                PadAccess(
                    net="N1",
                    ref="U1",
                    pad="1",
                    anchor_index=0,
                    anchor=(0.0, 0.0),
                    layers=("F.Cu",),
                    candidates=[
                        AccessSite("N1", "U1", "1", 0, 1.0, 0.0, "F.Cu", 1.0),
                        AccessSite("N1", "U1", "1", 0, 1.05, 0.02, "F.Cu", 0.9),
                        AccessSite("N1", "U1", "2", 1, 5.0, 0.0, "F.Cu", 1.0),
                    ],
                )
            ]
        },
        via_diameter_mm=0.6,
        via_drill_mm=0.3,
        clearance_mm=0.15,
    )
    shared = plan.shared_escape_resources(merge_mm=0.15)
    assert shared["raw_sites"] == 3
    assert shared["shared_resources"] == 2  # two close sites merge
    assert shared["savings"] == 1
    d = plan.to_dict()
    assert "shared_escapes" in d["metrics"]


def test_rules_profile_via_sizes():
    base = default_design_rules()
    p45 = apply_rules_profile(base.model_copy(deep=True), "via_0p45")
    p60 = apply_rules_profile(base.model_copy(deep=True), "via_0p6")
    assert p45.constraints.min_via_diameter_mm == pytest.approx(0.45)
    assert p45.constraints.min_via_drill_mm == pytest.approx(0.20)
    assert p60.constraints.min_via_diameter_mm == pytest.approx(0.60)
    assert p60.constraints.min_via_drill_mm == pytest.approx(0.30)

    src = apply_rules_profile(base.model_copy(deep=True), "source")
    assert any("source project" in n for n in (src.notes or []))


def test_pin_access_metrics_on_simple_fixture():
    board = load_board_from_kicad_pcb(FIXTURE)
    rules = load_design_rules(pcb_path=FIXTURE, manufacturer=None)
    m = pin_access_metrics(board, rules)
    assert "via_diameter_mm" in m
    assert "shared_escapes" in m
    assert m["shared_escapes"]["raw_sites"] >= 0


def test_pin_access_via_profile_changes_reachability():
    """Smaller vias should never be *worse* for inner reachability count."""
    board = load_board_from_kicad_pcb(FIXTURE)
    r60 = apply_rules_profile(
        load_design_rules(pcb_path=FIXTURE, manufacturer=None), "via_0p6"
    )
    r45 = apply_rules_profile(
        load_design_rules(pcb_path=FIXTURE, manufacturer=None), "via_0p45"
    )
    p60 = build_pin_access_plan(board, r60)
    p45 = build_pin_access_plan(board, r45)
    a60 = int(p60.metrics.get("inner_reachable_anchors") or 0)
    a45 = int(p45.metrics.get("inner_reachable_anchors") or 0)
    assert a45 >= a60


def test_cbs_repair_api_on_crossing_routes():
    from physics_router.conflict_cbs import detect_conflicts, repair_route_conflicts

    board = BoardModel(
        width_mm=20,
        height_mm=10,
        copper_layers=["F.Cu", "B.Cu"],
        components={
            "A": Component(ref="A", x_mm=2, y_mm=5, width_mm=1, height_mm=1, pads=[]),
            "B": Component(ref="B", x_mm=18, y_mm=5, width_mm=1, height_mm=1, pads=[]),
            "C": Component(ref="C", x_mm=10, y_mm=1, width_mm=1, height_mm=1, pads=[]),
            "D": Component(ref="D", x_mm=10, y_mm=9, width_mm=1, height_mm=1, pads=[]),
        },
        nets={"H": [("A", "1"), ("B", "1")], "V": [("C", "1"), ("D", "1")]},
    )
    # Crossing foreign nets on same layer
    result = RouteResult(
        segments=[
            RouteSegment(2, 5, 18, 5, "F.Cu", "H", 0.3),
            RouteSegment(10, 1, 10, 9, "F.Cu", "V", 0.3),
        ],
        via_count=0,
        total_length_mm=24.0,
    )
    confs = detect_conflicts(result, clearance_mm=0.2)
    assert len(confs) >= 1
    repaired, log = repair_route_conflicts(
        result, board, clearance_mm=0.2, max_clusters=2, max_cluster_size=4
    )
    assert "initial_conflicts" in log
    assert isinstance(repaired.segments, list)


def test_evaluate_board_hard_deadline_inline_zero_timeout(tmp_path: Path):
    """timeout_s=0 disables process spawn and routes inline."""
    row = evaluate_board(
        {
            "id": "simple_2net",
            "pcb": str(FIXTURE),
            "expect": "manufacturing_gate",
            "timeout_s": 0,
            "hard_deadline": True,
            "cbs_repair": False,
            "_base": str(ROOT),
        },
        out_dir=tmp_path / "z",
        hard_deadline=True,
        cbs_repair=False,
        effort=0.35,
    )
    assert not row.get("skipped")
    assert row.get("error") is None
    assert row.get("completion_ratio") == 1.0
    assert row.get("hard_violations") == 0
    assert row.get("passed") is True


def test_ci_manifest_extract_and_route(tmp_path: Path):
    assert CI_MANIFEST.is_file()
    # extract-only always
    summary = run_suite(
        CI_MANIFEST,
        out_dir=tmp_path / "ci_ext",
        extract_only=True,
    )
    assert summary["counts"]["total"] == 1
    assert summary["passed"] is True

    summary2 = run_suite(
        CI_MANIFEST,
        out_dir=tmp_path / "ci_rt",
        extract_only=False,
        hard_deadline=True,
        cbs_repair=False,
        effort=0.4,
    )
    assert summary2["counts"]["failed"] == 0
    board = summary2["boards"][0]
    assert board.get("passed") is True
    assert board.get("completion_ratio") == 1.0


def test_zone_fixture_file_committed(zone_pcb: Path):
    assert zone_pcb.is_file()
    assert zone_pcb.stat().st_size > 100
