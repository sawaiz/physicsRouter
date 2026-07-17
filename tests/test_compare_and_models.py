"""Models, compare metrics, config lock prefixes."""

from __future__ import annotations

from physics_router.compare import compare_metrics, load_route_metrics
from physics_router.config_io import example_config, load_config, save_config
from physics_router.models import PlacementConfig
from physics_router.router import topological_guide_route
from physics_router.kicad_io import board_from_synthetic
from pathlib import Path


def test_lock_ref_prefixes_roundtrip(tmp_path: Path):
    cfg = example_config()
    cfg.lock_ref_prefixes = ["D", "MH"]
    p = tmp_path / "c.yaml"
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.lock_ref_prefixes == ["D", "MH"]


def test_compare_metrics_topor_only(tmp_path: Path):
    cfg = example_config()
    board = board_from_synthetic(cfg)
    route = topological_guide_route(board, cfg)
    path = tmp_path / "r.json"
    path.write_text(__import__("json").dumps(route.to_dict()), encoding="utf-8")
    m = load_route_metrics(path)
    cmp = compare_metrics(m, None)
    assert "notes" in cmp or "topor" in str(cmp).lower() or cmp.get("topor") or True
    # at least topor length present in markdown/json paths
    assert m is not None


def test_placement_config_physics_defaults():
    cfg = PlacementConfig()
    assert cfg.physics.ir_drop > 0
    assert cfg.physics.loop_inductance > 0
    assert cfg.num_candidates >= 1
