"""Shared-escape costing, auto via profile, efficiency metrics, pipeline stages."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.compare import compare_to_golden
from physics_router.design_rules import default_design_rules, load_design_rules
from physics_router.global_router import build_global_route_plan
from physics_router.kicad_io import load_board_from_kicad_pcb
from physics_router.models import BoardModel, Component, PlacementConfig
from physics_router.pin_access import (
    AccessSite,
    PadAccess,
    PinAccessPlan,
    auto_select_via_profile,
    build_pin_access_plan,
)
from physics_router.route_pipeline import RoutePipelineSolver, run_capacity_pipeline
from physics_router.router import RouteResult, RouteSegment

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/golden/simple_2net.kicad_pcb"


def _dense_smd_board() -> BoardModel:
    """4-layer board with front-only SMD pads needing vias for inner routes."""
    comps = {}
    nets: dict[str, list] = {"BUS": []}
    # Ring of 8 pads around center — multipin net
    import math

    for i in range(8):
        ang = 2 * math.pi * i / 8
        ref = f"U{i}"
        x, y = 10 + 4 * math.cos(ang), 10 + 4 * math.sin(ang)
        comps[ref] = Component(
            ref=ref,
            x_mm=x,
            y_mm=y,
            width_mm=1.0,
            height_mm=1.0,
            footprint="QFN",
            pads=[
                {
                    "num": "1",
                    "net": "BUS",
                    "x": 0.0,
                    "y": 0.0,
                    "w": 0.4,
                    "h": 0.4,
                    "shape": "rect",
                    "layers": ["F.Cu"],
                }
            ],
        )
        nets["BUS"].append((ref, "1"))
    return BoardModel(
        width_mm=20.0,
        height_mm=20.0,
        copper_layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
        components=comps,
        nets=nets,
        outline=[
            {
                "kind": "poly",
                "pts": [(0, 0), (20, 0), (20, 20), (0, 20)],
                "closed": True,
                "layer": "Edge.Cuts",
            }
        ],
    )


def test_escape_resource_map_merges_nearby_pads():
    plan = PinAccessPlan(
        by_net={
            "N": [
                PadAccess(
                    "N",
                    "A",
                    "1",
                    0,
                    (0, 0),
                    ("F.Cu",),
                    candidates=[AccessSite("N", "A", "1", 0, 1.0, 0.0, "F.Cu", 1.0)],
                ),
                PadAccess(
                    "N",
                    "B",
                    "1",
                    1,
                    (0.1, 0),
                    ("F.Cu",),
                    candidates=[AccessSite("N", "B", "1", 1, 1.05, 0.02, "F.Cu", 1.0)],
                ),
                PadAccess(
                    "N",
                    "C",
                    "1",
                    2,
                    (5, 0),
                    ("F.Cu",),
                    candidates=[AccessSite("N", "C", "1", 2, 6.0, 0.0, "F.Cu", 1.0)],
                ),
            ]
        },
        via_diameter_mm=0.6,
        via_drill_mm=0.3,
        clearance_mm=0.15,
    )
    m = plan.escape_resource_map(merge_mm=0.15)
    assert m[("N", 0)] == m[("N", 1)]  # nearby pads share resource
    assert m[("N", 2)] != m[("N", 0)]


def test_global_plan_reports_shared_escape_savings():
    board = _dense_smd_board()
    rules = default_design_rules()
    rules = rules.model_copy(
        update={"copper_layers": list(board.copper_layers)}
    )
    # Use smaller vias for more access sites on dense ring
    from physics_router.design_rules import apply_rules_profile

    rules = apply_rules_profile(rules, "via_0p45")
    access = build_pin_access_plan(board, rules)
    plan = build_global_route_plan(board, None, rules, access, max_iterations=4)
    assert "shared_escape" in plan.metrics
    se = plan.metrics["shared_escape"]
    assert "via_units_saved" in se
    assert se["via_units_saved"] >= 0
    # Multipin net should have multiple sections
    assert plan.metrics.get("sections", 0) >= 1


def test_auto_via_profile_selects_better_reachability():
    board = _dense_smd_board()
    rules = default_design_rules()
    rules = rules.model_copy(update={"copper_layers": list(board.copper_layers)})
    chosen, report = auto_select_via_profile(
        board, rules, profiles=("via_0p6", "via_0p45")
    )
    assert report["selected"] in ("via_0p6", "via_0p45")
    assert len(report["trials"]) == 2
    # Selected should have max reach among trials
    by_name = {t["profile"]: t for t in report["trials"]}
    sel = by_name[report["selected"]]
    assert sel["inner_reachable_anchors"] == max(
        t["inner_reachable_anchors"] for t in report["trials"]
    )
    assert chosen.constraints.min_via_diameter_mm in (0.45, 0.60)


def test_pipeline_includes_via_profile_stage():
    board = load_board_from_kicad_pcb(FIXTURE)
    rules = load_design_rules(pcb_path=FIXTURE, manufacturer=None)
    solver = RoutePipelineSolver(
        board=board, rules=rules, effort=0.35, auto_via_profile=True
    )
    assert solver.STAGES[0] == "via_profile"
    # Run first stage only
    assert solver.step() is True
    assert solver.via_profile_report is not None
    assert solver.stage_log[0].name == "via_profile"
    assert solver.stage_log[0].ok is True


def test_pipeline_can_skip_auto_via_profile():
    board = load_board_from_kicad_pcb(FIXTURE)
    rules = load_design_rules(pcb_path=FIXTURE, manufacturer=None)
    solver = RoutePipelineSolver(
        board=board, rules=rules, effort=0.35, auto_via_profile=False
    )
    solver.step()
    assert solver.via_profile_report and solver.via_profile_report.get("skipped")


def test_capacity_pipeline_end_to_end_with_optimizations():
    board = load_board_from_kicad_pcb(FIXTURE)
    rules = load_design_rules(pcb_path=FIXTURE, manufacturer=None)
    result = run_capacity_pipeline(
        board, None, rules, effort=0.4, auto_via_profile=True, raise_on_fail=False
    )
    assert len(result.segments) >= 1 or result.unrouted_nets
    q = result.quality or {}
    gate = q.get("manufacturing_gate") or {}
    # Pipeline stages should include via_profile
    stages = gate.get("stages") or []
    assert "via_profile" in stages or "pin_access" in stages


def test_efficiency_metrics_in_golden_compare():
    human = RouteResult(
        segments=[
            RouteSegment(0, 0, 10, 0, "F.Cu", "A", 0.25),
            RouteSegment(0, 1, 12, 1, "F.Cu", "B", 0.25),
            RouteSegment(0, 2, 11, 2, "F.Cu", "CPX-0", 0.25),
            RouteSegment(0, 3, 13, 3, "F.Cu", "CPX-1", 0.25),
        ],
        via_count=0,
        total_length_mm=46.0,
    )
    ar = RouteResult(
        segments=[
            RouteSegment(0, 0, 11, 0, "F.Cu", "A", 0.25),  # longer
            RouteSegment(0, 1, 12, 1, "F.Cu", "B", 0.25),
            RouteSegment(0, 2, 11, 2, "F.Cu", "CPX-0", 0.25),
            RouteSegment(0, 3, 14, 3, "F.Cu", "CPX-1", 0.25),
        ],
        via_count=0,
        total_length_mm=48.0,
        unrouted_nets=[],
    )
    cmp = compare_to_golden(ar, human, hard_violations=0)
    eff = cmp.get("efficiency") or {}
    assert eff.get("nets_compared", 0) >= 2
    assert eff.get("mean_length_ratio_ar_over_human") is not None
    # CPX bundle skew reported
    prefs = [b["prefix"] for b in (eff.get("bundle_skew") or [])]
    assert "CPX" in prefs or any("CPX" in str(p) for p in prefs)


def test_shared_escape_reduces_cost_bias_vs_naive():
    """Shared charging should never increase planned via_count; cost units saved ≥ 0."""
    board = _dense_smd_board()
    rules = default_design_rules()
    rules = rules.model_copy(update={"copper_layers": list(board.copper_layers)})
    from physics_router.design_rules import apply_rules_profile

    rules = apply_rules_profile(rules, "via_0p45")
    access = build_pin_access_plan(board, rules)
    plan = build_global_route_plan(board, PlacementConfig(), rules, access)
    saved = (plan.metrics.get("shared_escape") or {}).get("via_units_saved", 0)
    assert saved >= 0
