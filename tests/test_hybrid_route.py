"""Hybrid multi-strategy classifier and routing (topological free-angle only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.config_io import example_config, load_config
from physics_router.design_rules import default_design_rules
from physics_router.hybrid_route import (
    _STRATEGY_ORDER,
    _matrix_order_variants,
    classify_board,
    hybrid_route,
)
from physics_router.kicad_io import board_from_synthetic, load_board_from_kicad_pcb
from physics_router.router import RouteResult, RouteSegment, clearance_aware_route

ROOT = Path(__file__).resolve().parents[1]
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
HALO_CFG = ROOT / "examples/halo-90/placement_config.yaml"


def test_classify_synthetic_has_strategies():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    plan = classify_board(board, cfg, default_design_rules())
    assert plan.assignments
    d = plan.to_dict()
    assert "by_strategy" in d
    assert sum(d["counts"].values()) == len(board.nets)


def test_matrix_order_variants_are_deterministic_and_complete():
    order = [f"CPX-{index}" for index in range(6)]
    variants = _matrix_order_variants(order)
    assert variants[0] == order
    assert variants[1] == list(reversed(order))
    assert 2 < len(variants) <= 4
    assert len({tuple(variant) for variant in variants}) == len(variants)
    assert all(
        set(variant) == set(order) and len(variant) == len(order)
        for variant in variants
    )


def test_small_bucket_variants_honor_limit():
    order = ["A", "B", "C"]
    variants = _matrix_order_variants(order, limit=2)
    assert variants == [order, list(reversed(order))]


def test_power_phase_reserves_plane_before_signal_buckets():
    assert _STRATEGY_ORDER[0] == "power"
    assert _STRATEGY_ORDER.index("critical") < _STRATEGY_ORDER.index("matrix")


@pytest.mark.skipif(
    not HALO_PCB.exists() or not HALO_CFG.exists(), reason="halo missing"
)
def test_classify_halo_matrix_cpx():
    cfg = load_config(HALO_CFG)
    board = load_board_from_kicad_pcb(HALO_PCB, cfg)
    plan = classify_board(board, cfg, default_design_rules())
    cpx = [a for a in plan.assignments if a.net.upper().startswith("CPX")]
    assert cpx
    assert all(a.strategy == "matrix" for a in cpx)
    powerish = [a for a in plan.assignments if a.net in ("+3V", "GND")]
    for a in powerish:
        assert a.strategy in ("power", "general", "critical")


def test_seed_result_and_nets_filter():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    nets = list(board.nets.keys())
    assert nets
    seed = RouteResult(segments=[RouteSegment(0, 0, 5, 0, "F.Cu", nets[0], 0.25)])
    rest = nets[1:] if len(nets) > 1 else nets
    r = clearance_aware_route(
        board,
        cfg,
        nets_filter=rest,
        seed_result=seed,
        prefer_native=False,
        soft_fallback=False,
        style="isotropic",
        skip_hybrid=True,
        grid_mm=0.5,
    )
    assert isinstance(r, RouteResult)


@pytest.mark.skipif(
    not HALO_PCB.exists() or not HALO_CFG.exists(), reason="halo missing"
)
def test_hybrid_route_halo_runs_fast():
    cfg = load_config(HALO_CFG)
    board = load_board_from_kicad_pcb(HALO_PCB, cfg)
    r = hybrid_route(board, cfg, default_design_rules())
    # Full-net zero-violation may leave sparse copper on dense matrix; pipeline must run
    assert (r.quality or {}).get("pipeline") == "hybrid"
    plan = (r.quality or {}).get("hybrid_plan") or {}
    assert "matrix" in (plan.get("by_strategy") or {})
    assert any("hybrid" in n for n in r.notes)
    # No halo_ring markers
    assert not any("halo_ring" in n or "concentric" in n for n in r.notes)
    # Legal: no shorts on whatever copper was committed
    from physics_router.router import native_drc_check

    rep = native_drc_check(r, clearance_mm=0.15, board=board)
    assert rep["shorts"] == 0


def test_no_halo_ring_module():
    import importlib.util

    assert importlib.util.find_spec("physics_router.halo_ring") is None
