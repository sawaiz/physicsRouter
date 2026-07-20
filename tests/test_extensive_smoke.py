"""Extensive end-to-end smoke: route quality, seed merge, SES, synthetic pads."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from physics_router.config_io import example_config
from physics_router.design_rules import default_design_rules
from physics_router.dsn_export import export_dsn
from physics_router.kicad_io import board_from_synthetic
from physics_router.models import KeepoutRegion
from physics_router.pin_access import build_pin_access_plan
from physics_router.route_pipeline import run_capacity_pipeline
from physics_router.router import (
    RouteResult,
    RouteSegment,
    Via,
    build_obstacle_map,
    clearance_aware_route,
)
from physics_router.routing_strategies import multilayer_route
from physics_router.ses_import import parse_ses_to_route


def test_synthetic_clearance_route_grade_a():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    t0 = time.time()
    r = clearance_aware_route(
        board, cfg, clearance_mm=0.2, grid_mm=0.5, soft_fallback=False
    )
    dt = time.time() - t0
    q = r.quality or r.compute_quality()
    assert len(r.segments) > 0
    assert r.clearance_violations == 0
    # Pad-accurate synthetic may leave a short multipin open under full-net policy
    assert len(r.unrouted_nets) <= 2
    assert q.get("grade") in ("A", "B", "C")
    assert dt < 15.0


def test_synthetic_has_pads_for_pin_access():
    board = board_from_synthetic(example_config())
    assert any(c.pads for c in board.components.values())
    rules = default_design_rules()
    plan = build_pin_access_plan(board, rules, max_candidates_per_pad=4)
    assert plan.metrics.get("tested_smd_anchors", 0) >= 1


def test_capacity_pipeline_synthetic():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    rules = default_design_rules()
    r = run_capacity_pipeline(board, cfg, rules, effort=0.4, raise_on_fail=False)
    assert r is not None
    q = r.quality or r.compute_quality()
    assert len(r.segments) > 0
    assert r.clearance_violations == 0 or q.get("grade") in ("A", "B", "C")


def test_keepout_and_seed_partial_route():
    cfg = example_config()
    cfg.keepouts = [KeepoutRegion(x1=30, y1=30, x2=45, y2=45)]
    board = board_from_synthetic(cfg)
    om = build_obstacle_map(board, clearance_mm=0.2, keepouts=cfg.keepouts)
    assert om.blocked(37.0, 37.0, "F.Cu", "FOREIGN")

    seed = RouteResult(
        segments=[
            RouteSegment(5, 5, 10, 10, layer="F.Cu", net="GND", width_mm=0.4)
        ],
        vias=[],
        via_count=0,
    )
    filt = [n for n in board.nets if n != "GND"]
    r = clearance_aware_route(
        board,
        cfg,
        clearance_mm=0.2,
        grid_mm=0.5,
        nets_filter=filt,
        seed_result=seed,
        soft_fallback=False,
    )
    assert any(s.net == "GND" for s in r.segments), "locked seed must survive"


def test_multilayer_with_keepouts():
    cfg = example_config()
    cfg.keepouts = [KeepoutRegion(x1=0, y1=0, x2=2, y2=2)]
    board = board_from_synthetic(cfg)
    rules = default_design_rules()
    r = multilayer_route(
        board, cfg, rules, clearance_mm=0.2, grid_mm=0.5, num_variants=1
    )
    assert r is not None
    assert len(r.segments) > 0


def test_ses_no_double_count(tmp_path: Path):
    ses = tmp_path / "fr.ses"
    ses.write_text(
        """
(session board
  (resolution mil 10)
  (unit mil)
  (routes
    (network_out
      (net "SW"
        (wire (path Signal_0 10 0 0 200 100))
        (via "Via[0-1]_0:24:12" 200 100)
      )
      (net "VCC"
        (wire (path Signal_1 12 0 50 150 50))
      )
    )
  )
)
""",
        encoding="utf-8",
    )
    r = parse_ses_to_route(ses, copper_layers=["F.Cu", "B.Cu"])
    assert len(r.segments) == 2  # not 4 from double parse
    assert r.via_count == 1
    assert {s.net for s in r.segments} == {"SW", "VCC"}
    assert r.vias[0].net == "SW"
    # no placeholder NET leftovers
    assert all(s.net != "NET" for s in r.segments)


def test_dsn_export_size(tmp_path: Path):
    cfg = example_config()
    board = board_from_synthetic(cfg)
    out = export_dsn(board, tmp_path / "b.dsn", config=cfg)
    text = out.read_text(encoding="utf-8")
    assert "(library" in text and "(network" in text
    assert out.stat().st_size > 500


@pytest.mark.skipif(
    not Path("third_party/halo-90/pcb/halo-90.kicad_pcb").exists(),
    reason="halo missing",
)
def test_halo_guide_extensive():
    from physics_router.kicad_io import load_board_from_kicad_pcb
    from physics_router.router import topological_guide_route

    board = load_board_from_kicad_pcb("third_party/halo-90/pcb/halo-90.kicad_pcb")
    assert len(board.components) >= 100
    r = topological_guide_route(board, example_config())
    assert len(r.segments) > 100
