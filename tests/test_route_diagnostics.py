"""Tests for structured route diagnostics (grade-improvement logs)."""

from __future__ import annotations

from physics_router.route_diagnostics import (
    StageTimer,
    analyze_route_result,
    diagnostics_to_markdown,
    write_diagnostics,
)
from physics_router.router import NetRouteReport, RouteResult, RouteSegment


def _toy_ar(*, open_nets: list[str] | None = None) -> RouteResult:
    segs = [
        RouteSegment(
            x1=0, y1=0, x2=5, y2=0, width_mm=0.2, layer="F.Cu", net="A"
        ),
        RouteSegment(
            x1=0, y1=1, x2=3, y2=1, width_mm=0.2, layer="F.Cu", net="B"
        ),
    ]
    ar = RouteResult(
        segments=segs,
        unrouted_nets=list(open_nets or ["GND", "CH0", "MISO"]),
        notes=[
            "hybrid phase power: +10 segs +2 vias +1 areas unrouted=1",
            "hybrid phase general: +20 segs +4 vias +0 areas unrouted=3",
            "ripup(empty): CH0 vs A,B (attempt 1)",
            "ripup(empty): CH0 vs A (attempt 2)",
            "ripup(empty): GND vs B (attempt 1)",
            "ROUTE FAILED manufacturing gate: 2/5 complete nets, 0 native DRC violation(s)",
        ],
        quality={
            "pipeline": "capacity_mesh+hybrid",
            "manufacturing_gate": {
                "passed": False,
                "complete_nets": 2,
                "required_nets": 5,
            },
            "hybrid_plan": {
                "counts": {"power": 1, "critical": 0, "general": 4, "matrix": 0},
                "notes": ["strategy counts: power=1, general=4"],
            },
            "production_route_plan": {
                "metrics": {
                    "final_overflow": 12,
                    "overflow_history": [20, 15, 12],
                    "mesh_overflow_nodes": 40,
                    "planned_vias": 8,
                }
            },
        },
    )
    ar.net_reports = [
        NetRouteReport(net="A", status="ok"),
        NetRouteReport(net="B", status="ok"),
        NetRouteReport(net="GND", status="unrouted"),
        NetRouteReport(net="CH0", status="unrouted"),
        NetRouteReport(net="MISO", status="unrouted"),
    ]
    return ar


def test_analyze_categorizes_missing_and_ripups():
    ar = _toy_ar()
    rep = analyze_route_result(ar, board_id="toy")
    assert rep["kind"] == "route_diagnostics"
    assert rep["summary"]["unrouted_nets"] == 3
    cats = rep["missing_by_category"]
    assert "power_gnd" in cats
    assert "GND" in cats["power_gnd"]
    assert "analog_channel" in cats
    assert "digital_bus" in cats
    ids = {d["id"] for d in rep["difficulties"]}
    assert "incomplete_nets" in ids
    assert "power_gnd_open" in ids
    assert "ripup_exhausted" in ids
    assert "global_overflow" in ids
    assert rep["ripup"]["empty_events"] >= 2
    assert rep["phases"]
    assert rep["recommended_actions"]


def test_write_diagnostics_files(tmp_path):
    ar = _toy_ar()
    rep = analyze_route_result(ar, board_id="toy")
    paths = write_diagnostics(rep, tmp_path)
    assert (tmp_path / "route_diagnostics.json").is_file()
    assert (tmp_path / "route_diagnostics.md").is_file()
    md = diagnostics_to_markdown(rep)
    assert "Route diagnostics" in md
    assert "Recommended actions" in md
    assert paths["json"].endswith("route_diagnostics.json")


def test_stage_timer_appends(tmp_path):
    p = tmp_path / "stage_log.json"
    t = StageTimer(p)
    t.mark("via_profile")
    t.mark("pin_access", detail={"sites": 10})
    assert len(t.events) == 2
    assert p.is_file()
    data = p.read_text(encoding="utf-8")
    assert "pin_access" in data
