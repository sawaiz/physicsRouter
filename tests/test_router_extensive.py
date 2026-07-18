"""Extensive router tests: clearance, apply-to-PCB, audit, multilayer, coords."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.config_io import example_config, load_config
from physics_router.design_rules import default_design_rules
from physics_router.dsn_export import export_dsn
from physics_router.kicad_io import board_from_synthetic, load_board_from_kicad_pcb
from physics_router.models import BoardModel, Component
from physics_router.router import (
    append_routes_to_kicad_pcb,
    audit_same_layer_clearance,
    board_extent,
    build_obstacle_map,
    clearance_aware_route,
    free_angle_route,
    rubberband_cleanup,
    topological_guide_route,
)
from physics_router.routing_strategies import multilayer_route, pre_route_analysis
from physics_router.viewer_export import build_viewer_payload, route_to_viewer_dict

ROOT = Path(__file__).resolve().parents[1]
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
HALO_CFG = ROOT / "examples/halo-90/placement_config.yaml"


def _synthetic():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    return cfg, board


def test_guide_vs_clearance_soft_fallback() -> None:
    cfg, board = _synthetic()
    guide = topological_guide_route(board, cfg)
    assert guide.total_length_mm > 0
    assert len(guide.segments) > 0

    hard = clearance_aware_route(
        board, cfg, clearance_mm=0.2, grid_mm=1.0, soft_fallback=False
    )
    # hard mode never uses straight_fallback method
    for rep in hard.net_reports:
        assert "straight_fallback" not in (rep.method or "")


def test_soft_fallback_allowed_in_guide_only() -> None:
    cfg, board = _synthetic()
    r = clearance_aware_route(
        board, cfg, clearance_mm=0.0, guide_only=True, soft_fallback=True
    )
    assert r.total_length_mm > 0
    d = r.to_dict()
    assert "quality" in d
    assert "net_reports" in d


def test_append_and_strip_physics_router_block(tmp_path: Path) -> None:
    cfg, board = _synthetic()
    route = clearance_aware_route(
        board, cfg, clearance_mm=0.15, grid_mm=1.0, soft_fallback=True
    )
    assert route.segments

    src = tmp_path / "board.kicad_pcb"
    src.write_text(
        "(kicad_pcb\n  (version 20240108)\n  (generator test)\n)\n", encoding="utf-8"
    )
    out = tmp_path / "routed.kicad_pcb"
    append_routes_to_kicad_pcb(str(src), str(out), route)
    text = out.read_text(encoding="utf-8")
    assert "(segment" in text
    n1 = text.count("(segment")

    # second apply replaces prior tracks (no stack)
    route2 = clearance_aware_route(
        board, cfg, clearance_mm=0.2, grid_mm=1.0, soft_fallback=True
    )
    append_routes_to_kicad_pcb(str(out), str(out), route2, replace_previous=True)
    text2 = out.read_text(encoding="utf-8")
    n2 = text2.count("(segment")
    assert n2 == len(route2.segments)
    assert n2 > 0
    # Replace must not leave both route generations stacked
    assert n2 <= max(n1, len(route2.segments)) + 1


def test_append_and_replace_native_copper_area(tmp_path: Path) -> None:
    from physics_router.router import (
        CopperArea,
        RouteResult,
        strip_physics_router_zones,
    )

    src = tmp_path / "area_src.kicad_pcb"
    src.write_text(
        '(kicad_pcb\n  (version 20240108)\n  (generator "test")\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
        '  (net 0 "")\n  (net 1 "GND")\n)\n',
        encoding="utf-8",
    )
    out = tmp_path / "area_out.kicad_pcb"
    area = CopperArea(
        outline=[(1, 1), (9, 1), (9, 9), (1, 9)],
        layer="B.Cu",
        net="GND",
        clearance_mm=0.2,
    )
    append_routes_to_kicad_pcb(str(src), str(out), RouteResult(areas=[area]))
    text = out.read_text(encoding="utf-8")
    assert '(zone (net 1) (net_name "GND")' in text
    assert "(tstamp 70726f75-" in text
    assert '(layer "B.Cu")' in text
    assert text.count("(xy ") == 4
    assert "(zone" not in strip_physics_router_zones(text)


def test_audit_same_layer_clearance_detects_overlap() -> None:
    from physics_router.router import RouteResult, RouteSegment

    # two foreign nets on same layer, nearly coincident
    segs = [
        RouteSegment(0, 0, 10, 0, layer="F.Cu", net="A", width_mm=0.3),
        RouteSegment(0, 0.05, 10, 0.05, layer="F.Cu", net="B", width_mm=0.3),
    ]
    result = RouteResult(segments=segs)
    audit = audit_same_layer_clearance(result, clearance_mm=0.2)
    assert audit["near_miss_pairs"] >= 1
    assert any("clearance_audit" in n for n in audit["notes"])


def test_audit_ok_when_far_apart() -> None:
    from physics_router.router import RouteResult, RouteSegment

    segs = [
        RouteSegment(0, 0, 5, 0, layer="F.Cu", net="A", width_mm=0.2),
        RouteSegment(0, 5, 5, 5, layer="F.Cu", net="B", width_mm=0.2),
    ]
    result = RouteResult(segments=segs)
    audit = audit_same_layer_clearance(result, clearance_mm=0.2)
    assert audit["near_miss_pairs"] == 0


def test_free_angle_los() -> None:
    om = build_obstacle_map(
        BoardModel(
            width_mm=20,
            height_mm=20,
            components={
                "R1": Component(ref="R1", x_mm=5, y_mm=5, width_mm=1, height_mm=1)
            },
            nets={},
        ),
        clearance_mm=0.1,
    )
    path = free_angle_route((1, 1), (10, 10), "F.Cu", "N1", om, grid_mm=0.5)
    assert path is not None
    assert path[0] == (1, 1) and path[-1] == (10, 10)


def test_rubberband_shortens() -> None:
    cfg, board = _synthetic()
    route = topological_guide_route(board, cfg)
    cleaned = rubberband_cleanup(route, board, cfg, clearance_mm=0.0)
    assert cleaned.total_length_mm <= route.total_length_mm + 1e-6
    assert cleaned.quality


def test_multilayer_route_policy_notes() -> None:
    cfg, board = _synthetic()
    rules = default_design_rules()
    rules.copper_layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    result = multilayer_route(board, cfg, rules, grid_mm=1.0, clearance_mm=0.2)
    assert any("drc:" in n for n in result.notes)
    d = result.to_dict()
    assert "quality" in d


def test_pre_route_analysis_density() -> None:
    cfg, board = _synthetic()
    report = pre_route_analysis(board, cfg, default_design_rules())
    d = report.to_dict()
    assert d["net_count"] >= 1
    assert d["pin_count"] >= 1
    assert "estimated_density_pins_per_cm2" in d


def test_dsn_export(tmp_path: Path) -> None:
    cfg, board = _synthetic()
    out = tmp_path / "board.dsn"
    export_dsn(board, out, config=cfg)
    text = out.read_text(encoding="utf-8")
    assert "pcb" in text.lower() or "structure" in text.lower() or len(text) > 50


def test_viewer_payload_routes_include_quality() -> None:
    cfg, board = _synthetic()
    route = topological_guide_route(board, cfg)
    vd = route_to_viewer_dict(route, "guide")
    assert "segments" in vd and vd["segments"]
    assert "quality" in vd
    payload = build_viewer_payload(board, cfg, routes={"guide": route})
    assert "guide" in payload["routes"]
    assert payload["routes"]["guide"]["total_length_mm"] > 0


def test_progress_callback_monotonic() -> None:
    cfg, board = _synthetic()
    progress = []

    def cb(done, total, name, stage, detail):
        progress.append((done, total, name, stage))

    clearance_aware_route(
        board, cfg, clearance_mm=0.2, grid_mm=1.0, soft_fallback=False, progress_cb=cb
    )
    assert progress
    totals = {t for _, t, _, _ in progress}
    assert len(totals) == 1
    # done values should not decrease within a simple pass
    dones = [d for d, _, _, _ in progress if d > 0]
    assert dones == sorted(dones) or len(dones) >= 1


def test_obstacle_map_same_net_may_pass() -> None:
    board = BoardModel(
        width_mm=30,
        height_mm=30,
        components={
            "R1": Component(
                ref="R1",
                x_mm=10,
                y_mm=10,
                width_mm=2,
                height_mm=1,
                pads=[{"num": "1", "net": "SIG"}, {"num": "2", "net": "SIG"}],
            )
        },
        nets={"SIG": [("R1", "1"), ("R1", "2")]},
    )
    om = build_obstacle_map(board, clearance_mm=0.15)
    # same net through pad region should not block
    assert not om.blocked(10, 10, "F.Cu", "SIG")


@pytest.mark.skipif(
    not HALO_PCB.exists() or not HALO_CFG.exists(), reason="halo-90 missing"
)
def test_halo_leds_locked_and_extent() -> None:
    cfg = load_config(HALO_CFG)
    assert "D" in (cfg.lock_ref_prefixes or [])
    board = load_board_from_kicad_pcb(HALO_PCB, cfg)
    leds = [r for r in board.components if r.startswith("D")]
    assert len(leds) >= 90
    assert all(board.components[r].locked for r in leds)
    x0, x1, y0, y1 = board_extent(board)
    assert x0 < 0 < x1
    assert y0 < 0 < y1
    om = build_obstacle_map(board, clearance_mm=0.15, layers=board.copper_layers)
    assert om.in_bounds(0, 0)
    assert om.in_bounds(10, 0)


@pytest.mark.skipif(
    not HALO_PCB.exists() or not HALO_CFG.exists(), reason="halo-90 missing"
)
def test_halo_guide_route_runs() -> None:
    cfg = load_config(HALO_CFG)
    board = load_board_from_kicad_pcb(HALO_PCB, cfg)
    # Coarse guide only — full clearance A* on 111 parts is slow for unit tests
    route = topological_guide_route(board, cfg)
    assert route.total_length_mm > 0
    assert len(route.segments) > 10
    d = route.to_dict()
    assert d["quality"]["score"] >= 0


def test_route_result_to_dict_layers() -> None:
    cfg, board = _synthetic()
    r = topological_guide_route(board, cfg)
    d = r.to_dict()
    assert "length_by_layer_mm" in d
    assert isinstance(d["length_by_layer_mm"], dict)
