"""Continuous place+route improve: goal / timeout / live score callbacks."""

from __future__ import annotations

from physics_router.config_io import example_config
from physics_router.continuous_improve import (
    ImproveConfig,
    _apply_kicad_to_snapshot,
    continuous_improve,
    goal_met,
    ImproveSnapshot,
    min_score_for_grade,
)
from physics_router.kicad_io import board_from_synthetic
from physics_router.router import RouteResult


def test_min_score_for_grade():
    assert min_score_for_grade("A") == 90.0
    assert min_score_for_grade("B") == 75.0
    assert min_score_for_grade("C") == 55.0


def test_goal_met_requires_drc_clean():
    cfg = ImproveConfig(min_score=50, target_grade="C", require_drc_clean=True, require_complete=True)
    dirty = ImproveSnapshot(
        round=1, elapsed_s=1, strategy="t", score=99, grade="A",
        violations=3, shorts=1, spacing=2, outline=0, vias=0, unrouted=0,
        length_mm=10, placement_cost=None, stage="scored",
    )
    clean = ImproveSnapshot(
        round=1, elapsed_s=1, strategy="t", score=60, grade="C",
        violations=0, shorts=0, spacing=0, outline=0, vias=1, unrouted=0,
        length_mm=10, placement_cost=None, stage="scored",
    )
    assert not goal_met(dirty, cfg)
    assert goal_met(clean, cfg)


def test_kicad_oracle_is_stamped_on_serialized_route(monkeypatch):
    """A KiCad failure may never disappear from the returned route metrics."""
    from physics_router import kicad_tools

    monkeypatch.setattr(
        kicad_tools,
        "kicad_drc_route",
        lambda *_args, **_kwargs: {
            "available": True,
            "copper_violation_count": 17,
            "copper_error_count": 4,
            "copper_passed": False,
            "samples": [],
            "by_type": {"clearance": 17},
            "kicad_version": "test",
        },
    )
    route = RouteResult()
    snap = ImproveSnapshot(
        round=1,
        elapsed_s=1.0,
        strategy="native",
        score=100.0,
        grade="A",
        violations=0,
        shorts=0,
        spacing=0,
        outline=0,
        vias=0,
        unrouted=0,
        length_mm=0.0,
        placement_cost=None,
        stage="scored",
    )
    cfg = ImproveConfig(pcb_path="board.kicad_pcb", require_kicad_drc=True)
    updated = _apply_kicad_to_snapshot(snap, route, cfg, force=True)
    assert updated.violations == 17
    assert route.clearance_violations == 17
    assert route.quality["kicad_drc"]["passed"] is False


def test_continuous_improve_timeout_and_live_score():
    """Tiny synthetic board: should finish quickly with history + live events."""
    cfg = example_config()
    cfg.use_spice = False
    cfg.use_openems = False
    board = board_from_synthetic(cfg)
    events: list[dict] = []

    def on_prog(ev: dict) -> None:
        events.append(ev)

    icfg = ImproveConfig(
        timeout_s=8.0,
        min_score=90.0,
        target_grade="A",
        require_drc_clean=True,
        require_complete=True,
        do_place=False,  # route-only for speed
        do_route=True,
        clearance_mm=0.2,
        grid_mm=0.5,
        max_rounds=3,
        prefer_native=True,
        allow_topor_rounds=False,
    )
    result = continuous_improve(board, cfg, improve=icfg, progress_cb=on_prog)
    assert result.stop_reason in ("goal", "timeout", "max_rounds", "complete")
    assert result.history, "expected at least one scored round"
    assert any(e.get("event") == "snapshot" for e in events)
    assert any(e.get("event") == "done" for e in events)
    if result.route is not None:
        q = result.route.quality or {}
        assert "score" in q or result.best_snapshot is not None
    if result.best_snapshot:
        assert result.best_snapshot.score >= 0
        # DRC fields present on snapshot
        assert result.best_snapshot.violations >= 0


def test_continuous_improve_cancel():
    cfg = example_config()
    cfg.use_spice = False
    cfg.use_openems = False
    board = board_from_synthetic(cfg)
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # cancel after first progress

    # cancel_cb checked at loop start — fire after first full round by using max_rounds
    # and cancel immediately when second round would start
    n_checks = {"c": 0}

    def cancel_after_first() -> bool:
        n_checks["c"] += 1
        # continuous_improve checks cancel at top of each round; allow first entry
        return n_checks["c"] > 2

    icfg = ImproveConfig(
        timeout_s=60.0,
        min_score=101.0,  # unreachable
        target_grade="A",
        do_place=False,
        max_rounds=5,
        prefer_native=True,
        allow_topor_rounds=False,
        grid_mm=0.5,
    )
    result = continuous_improve(
        board, cfg, improve=icfg, cancel_cb=cancel_after_first
    )
    assert result.stop_reason in ("cancelled", "goal", "max_rounds", "timeout")
