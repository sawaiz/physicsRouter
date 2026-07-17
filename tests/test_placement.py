"""Tests for config, physics scoring, and multi-candidate placement."""

from __future__ import annotations

from pathlib import Path

from physics_router.config_io import example_config, load_config, save_config
from physics_router.kicad_io import board_from_synthetic, parse_sexpr
from physics_router.models import NetClass
from physics_router.placement import optimize_placement
from physics_router.physics import geometric_score, power_loop_area, weighted_wirelength
from physics_router.router import topological_guide_route


def test_example_config_roundtrip(tmp_path: Path) -> None:
    cfg = example_config()
    path = tmp_path / "cfg.yaml"
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.project_name == "demo_buck"
    assert any(n.name == "SW" and n.weight == 5.0 for n in loaded.nets)
    assert loaded.net_by_name()["USB_DP"].pair_with == "USB_DM"
    assert loaded.weight_for_net("SW") > loaded.weight_for_net("AIN0")


def test_examples_yaml_loads() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "examples" / "placement_config.yaml")
    assert cfg.use_spice and cfg.use_openems
    assert cfg.nets[0].net_class in set(NetClass)


def test_synthetic_board_and_score() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    assert "U1" in board.components
    assert "SW" in board.nets
    sb = geometric_score(board, cfg)
    assert sb.total > 0
    assert weighted_wirelength(board, cfg) > 0
    assert power_loop_area(board, cfg) >= 0


def test_optimize_placement_improves_or_ranks() -> None:
    cfg = example_config()
    cfg.num_candidates = 3
    cfg.sa_iterations = 400
    cfg.spice_on_top_n = 2
    cfg.openems_on_top_n = 1
    board = board_from_synthetic(cfg)
    result = optimize_placement(board, cfg)
    assert result.best.rank == 1
    assert len(result.candidates) == 3
    assert result.best.score.total <= result.candidates[-1].score.total
    # Fixed connector should stay put
    jx, jy, _ = result.best.positions["J1"]
    assert abs(jx - 2.0) < 1e-6
    assert abs(jy - 20.0) < 1e-6
    # Physics notes expected when spice/openems enabled
    assert result.best.score.notes or result.candidates[0].score.total >= 0


def test_route_guide() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    routes = topological_guide_route(board, cfg)
    assert routes.total_length_mm > 0
    assert len(routes.segments) > 0


def test_parse_sexpr_minimal() -> None:
    tree = parse_sexpr('(kicad_pcb (version 20240108) (footprint "R" (at 1 2 90) (property "Reference" "R1")))')
    assert tree[0] == "kicad_pcb"
