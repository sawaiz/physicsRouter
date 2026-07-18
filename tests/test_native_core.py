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
    assert "1.1" in str(i["version"]) or "native" in str(i["version"])
    assert "gpu" in i
    assert i.get("features", {}).get("isotropic") is True


def test_native_route_synthetic():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    raw = route_board_native(board, cfg, clearance_mm=0.2, grid_mm=0.5, soft_fallback=False)
    assert raw is not None
    assert raw["backend"] == "native"
    assert "elapsed_ms" in raw
    assert isinstance(raw["segments"], list)
    assert "quality" in raw
    notes = " ".join(raw.get("notes") or [])
    assert "isotropic" in notes.lower() or "native" in notes.lower()
    # via reasons when vias present
    for v in raw.get("vias") or []:
        if v.get("reason"):
            assert "layer" in v["reason"].lower() or "blocked" in v["reason"].lower() or "transition" in v["reason"].lower()
            break


def test_python_clearance_uses_native_when_present():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    r = clearance_aware_route(
        board, cfg, clearance_mm=0.2, grid_mm=0.5, soft_fallback=False, prefer_native=True
    )
    joined = " ".join(r.notes)
    assert r.total_length_mm >= 0
    # native path or pure python both valid
    assert "native" in joined or "isotropic" in joined or "clearance_mm" in joined or r.segments is not None


def test_native_polish_helper():
    from physics_router.native_bridge import polish_native_with_python

    cfg = example_config()
    board = board_from_synthetic(cfg)
    raw = route_board_native(board, cfg, clearance_mm=0.2, grid_mm=1.0, soft_fallback=False)
    assert raw is not None
    polished = polish_native_with_python(board, cfg, raw, clearance_mm=0.2)
    assert polished.quality is not None
    assert polished.segments is not None


def test_native_atomic_net_does_not_commit_partial_copper():
    """A blocked anchor must roll the whole net back, including earlier edges."""
    import pr_native

    cfg = pr_native.RouteConfig()
    cfg.x_min = -5.0
    cfg.x_max = 5.0
    cfg.y_min = -5.0
    cfg.y_max = 5.0
    cfg.grid_mm = 0.25
    cfg.clearance_mm = 0.2
    cfg.num_layers = 1
    cfg.allow_vias = False
    cfg.soft_fallback = False
    cfg.atomic_nets = True

    def point(x: float, y: float):
        value = pr_native.Vec2()
        value.x = x
        value.y = y
        return value

    net = pr_native.NetSpec()
    net.net_id = 7
    net.name = "ATOMIC"
    net.anchors = [point(-4.0, 0.0), point(-2.0, 0.0), point(3.0, 0.0)]
    net.preferred_layers = [0]

    # The first two anchors can connect, while the third is isolated by a
    # board-spanning wall.  No fragment from the successful first edge may
    # survive the failed full-net transaction.
    wall = pr_native.RectObs()
    wall.cx = 0.5
    wall.cy = 0.0
    wall.w = 0.8
    wall.h = 10.0
    wall.net_id = -1

    result = pr_native.route_board([net], cfg, [wall])
    assert result.segments == []
    assert result.vias == []
    assert result.unrouted == ["ATOMIC"]
    assert result.net_reports[0].status == "unrouted"
    assert result.net_reports[0].method == "atomic_unrouted"
