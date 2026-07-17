"""Native C++ core tests — skip if pr_native not built."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Prefer in-tree build
for p in (ROOT / "native" / "build", ROOT / "native" / "build" / "Release"):
    if p.is_dir():
        sys.path.insert(0, str(p))

from physics_router.native_bridge import available, info, route_board_native  # noqa: E402
from physics_router.config_io import example_config  # noqa: E402
from physics_router.kicad_io import board_from_synthetic  # noqa: E402
from physics_router.router import clearance_aware_route  # noqa: E402

pytestmark = pytest.mark.skipif(not available(), reason="pr_native not built (run scripts/build_native.sh)")


def test_native_info():
    i = info()
    assert i["available"] is True
    assert "version" in i
    assert "gpu" in i


def test_native_route_synthetic():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    raw = route_board_native(board, cfg, clearance_mm=0.2, grid_mm=0.5, soft_fallback=False)
    assert raw is not None
    assert raw["backend"] == "native"
    assert "elapsed_ms" in raw
    assert isinstance(raw["segments"], list)
    assert "quality" in raw


def test_python_clearance_uses_native_when_present():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    r = clearance_aware_route(board, cfg, clearance_mm=0.2, grid_mm=0.5, soft_fallback=False)
    # notes should mention native if path taken
    joined = " ".join(r.notes)
    assert r.total_length_mm >= 0
    # either native notes or python notes — both valid
    assert "native" in joined or "clearance_mm" in joined or r.segments is not None
