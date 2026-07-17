"""Tests for IR drop, loop L, return path, matrix match, rubberband cleanup."""

from __future__ import annotations

from physics_router.config_io import example_config
from physics_router.kicad_io import board_from_synthetic
from physics_router.physics import (
    geometric_score,
    ir_drop_proxy,
    loop_inductance_nh,
    matrix_length_match_score,
    return_path_score,
)
from physics_router.router import clearance_aware_route, rubberband_cleanup, topological_guide_route


def test_new_physics_terms_present() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    sb = geometric_score(board, cfg)
    d = sb.as_dict()
    for key in ("ir_drop", "loop_inductance", "return_path", "matrix_length_match"):
        assert key in d
    assert sb.notes  # physics notes from new terms
    ir, _ = ir_drop_proxy(board, cfg)
    lnh, _ = loop_inductance_nh(board, cfg)
    assert ir >= 0 and lnh >= 0
    rp, _ = return_path_score(board, cfg)
    mx, note = matrix_length_match_score(board, cfg)
    assert rp >= 0 and mx >= 0


def test_rubberband_cleanup_shortens_or_equal() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    routes = topological_guide_route(board, cfg)
    cleaned = rubberband_cleanup(routes, board, cfg, clearance_mm=0.0)
    assert cleaned.total_length_mm <= routes.total_length_mm + 1e-6
    assert any("rubberband_cleanup" in n for n in cleaned.notes)


def test_clearance_route_then_cleanup() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    routes = clearance_aware_route(board, cfg, clearance_mm=0.2, grid_mm=1.0)
    cleaned = rubberband_cleanup(routes, board, cfg, clearance_mm=0.2)
    assert cleaned.segments
    assert cleaned.total_length_mm > 0
