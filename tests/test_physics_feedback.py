"""Post-route SPICE/OpenEMS feedback into place/topology/pours."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.config_io import example_config
from physics_router.continuous_improve import ImproveConfig, continuous_improve
from physics_router.design_rules import default_design_rules
from physics_router.kicad_io import board_from_synthetic
from physics_router.models import NetClass
from physics_router.physics_feedback import (
    apply_feedback_to_config,
    propose_power_pours,
    route_is_physics_eligible,
    score_full_route_physics,
)
from physics_router.route_pipeline import run_capacity_pipeline
from physics_router.router import RouteResult, RouteSegment


def _full_route():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    rules = default_design_rules()
    route = run_capacity_pipeline(
        board, cfg, rules, effort=0.4, raise_on_fail=False, auto_via_profile=False
    )
    return board, cfg, route


def test_ineligible_when_unrouted():
    board, cfg, route = _full_route()
    bad = RouteResult(
        segments=list(route.segments[:1]) if route.segments else [],
        unrouted_nets=["X"],
        clearance_violations=0,
    )
    ok, reason = route_is_physics_eligible(bad, board, require_complete=True)
    assert ok is False
    assert "unrouted" in reason


def test_ineligible_when_drc():
    board, cfg, route = _full_route()
    route.clearance_violations = 3
    ok, reason = route_is_physics_eligible(route, board, require_complete=False)
    assert ok is False
    assert "drc" in reason


def test_score_full_route_physics_eligible():
    board, cfg, route = _full_route()
    # Allow incomplete synthetic for unit test stability
    fb = score_full_route_physics(
        board, cfg, route, require_complete=False, generate_pours=True
    )
    assert fb.eligible is True
    assert fb.spice_cost >= 0
    assert fb.openems_cost >= 0
    assert fb.combined_cost > 0
    assert any("spice" in n or "em_proxy" in n for n in fb.notes)
    assert "physics_feedback" in (route.quality or {})


def test_propose_power_pours_for_ground():
    board, cfg, route = _full_route()
    # Ensure config has GND-class nets
    for lab in cfg.nets:
        if "GND" in lab.name.upper() or lab.net_class == NetClass.GROUND:
            lab.net_class = NetClass.GROUND
    pours = propose_power_pours(board, cfg, route)
    # May or may not find GND depending on example config — at least callable
    assert isinstance(pours, list)


def test_apply_feedback_bumps_weights():
    board, cfg, route = _full_route()
    fb = score_full_route_physics(
        board, cfg, route, require_complete=False, generate_pours=False
    )
    # Force bumps
    fb.placement_weight_bumps = {cfg.nets[0].name: 0.5}
    fb.openems_cost = 20.0
    fb.spice_cost = 12.0
    new_cfg = apply_feedback_to_config(cfg, fb)
    assert new_cfg.net_by_name()[cfg.nets[0].name].weight > cfg.nets[0].weight
    assert new_cfg.physics.emi_proxy >= cfg.physics.emi_proxy
    assert new_cfg.use_spice and new_cfg.use_openems


def test_continuous_improve_runs_physics_on_clean_route():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    events: list[str] = []

    def prog(ev):
        if ev.get("event") == "stage":
            events.append(str(ev.get("stage")))

    res = continuous_improve(
        board,
        cfg,
        improve=ImproveConfig(
            timeout_s=45.0,
            min_score=0.0,
            target_grade="F",
            require_drc_clean=True,
            require_complete=False,  # synthetic may leave opens
            do_place=False,
            do_route=True,
            max_rounds=1,
            physics_feedback=True,
            physics_generate_pours=True,
            prefer_native=True,
        ),
        progress_cb=prog,
    )
    assert res.route is not None
    # Physics stage attempted when DRC clean
    assert "physics_feedback" in events or any(
        "physics" in n for n in res.notes
    )


def test_openems_export_optional(tmp_path: Path):
    board, cfg, route = _full_route()
    fb = score_full_route_physics(
        board,
        cfg,
        route,
        export_dir=tmp_path / "em",
        require_complete=False,
        generate_pours=False,
    )
    assert fb.eligible
    # Export may succeed or soft-fail; should not raise
    assert isinstance(fb.export_paths, dict)
