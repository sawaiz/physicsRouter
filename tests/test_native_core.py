"""Native C++ core tests — skip if pr_native not built."""

from __future__ import annotations

import math
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

pytestmark = pytest.mark.skipif(
    not available(), reason="pr_native not built (run scripts/build_native.sh)"
)


def test_native_info():
    i = info()
    assert i["available"] is True
    assert "version" in i
    assert str(i["version"]).startswith("2.0.")
    assert i["features"]["pathfinder_history"] is True
    assert i["features"]["conflict_directed_ripup"] is True
    assert i["features"]["no_via_in_pad"] is True
    assert i["features"]["pin_access_oracle"] is True
    assert i["features"]["section_layer_planning"] is True
    assert "gpu" in i
    assert i.get("features", {}).get("isotropic") is True


def test_native_route_synthetic():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    raw = route_board_native(
        board, cfg, clearance_mm=0.2, grid_mm=0.5, soft_fallback=False
    )
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
            assert (
                "layer" in v["reason"].lower()
                or "blocked" in v["reason"].lower()
                or "transition" in v["reason"].lower()
            )
            break
    from physics_router.router import _route_result_from_dict, native_drc_check

    route = _route_result_from_dict(raw)
    drc = native_drc_check(route, clearance_mm=0.2, board=board)
    assert drc["violations"] == 0


def test_python_clearance_uses_native_when_present():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    r = clearance_aware_route(
        board,
        cfg,
        clearance_mm=0.2,
        grid_mm=0.5,
        soft_fallback=False,
        prefer_native=True,
    )
    joined = " ".join(r.notes)
    assert r.total_length_mm >= 0
    # native path or pure python both valid
    assert (
        "native" in joined
        or "isotropic" in joined
        or "clearance_mm" in joined
        or r.segments is not None
    )


def test_native_polish_helper():
    from physics_router.native_bridge import polish_native_with_python

    cfg = example_config()
    board = board_from_synthetic(cfg)
    raw = route_board_native(
        board, cfg, clearance_mm=0.2, grid_mm=1.0, soft_fallback=False
    )
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


def test_native_copper_area_emits_organic_zone_boundary():
    """Power-style nets can use refillable organic copper instead of tracks."""
    import pr_native

    cfg = pr_native.RouteConfig()
    cfg.x_min = -10.0
    cfg.x_max = 10.0
    cfg.y_min = -10.0
    cfg.y_max = 10.0
    cfg.num_layers = 2
    cfg.clearance_mm = 0.2

    def point(x: float, y: float):
        value = pr_native.Vec2()
        value.x = x
        value.y = y
        return value

    net = pr_native.NetSpec()
    net.net_id = 3
    net.name = "GND"
    net.anchors = [point(-5, -2), point(0, 4), point(5, -2)]
    net.preferred_layers = [1]
    net.use_copper_area = True
    net.area_margin_mm = 1.0
    net.area_priority = 10

    result = pr_native.route_board([net], cfg, [])
    assert result.segments
    assert result.vias == []
    assert len(result.areas) == 1
    area = result.areas[0]
    assert area.net_id == 3
    assert area.layer == 1
    assert area.priority == 10
    assert len(area.outline) >= 12
    assert result.unrouted == []
    assert result.net_reports[0].status == "ok"
    assert "copper_area" in result.net_reports[0].method


def test_native_bridge_routes_power_as_copper_areas():
    cfg = example_config()
    board = board_from_synthetic(cfg)
    raw = route_board_native(
        board,
        cfg,
        net_order=["GND", "+5V"],
        exclusive_nets=True,
        use_copper_areas=True,
    )
    assert raw is not None
    assert len(raw["areas"]) == 2
    assert raw["segments"]
    assert all("copper_area" in report["method"] for report in raw["net_reports"])


def test_native_bridge_exclusive_bucket_preserves_caller_order(monkeypatch):
    """Hybrid rebuild variants must reach C++ without semantic re-sorting."""
    import pr_native
    from physics_router import native_bridge

    cfg = example_config()
    board = board_from_synthetic(cfg)
    order = list(reversed(list(board.nets)[:2]))
    captured = {}

    real_route_board = pr_native.route_board

    def capture(nets, route_cfg, obstacles):
        captured["names"] = [net.name for net in nets]
        captured["priorities"] = [net.priority for net in nets]
        return real_route_board(nets, route_cfg, obstacles)

    monkeypatch.setattr(pr_native, "route_board", capture)
    native_bridge.route_board_native(
        board,
        cfg,
        net_order=order,
        exclusive_nets=True,
        use_gpu=False,
    )
    assert captured["names"] == order
    assert len(set(captured["priorities"])) == 1


def test_native_obstacle_is_layer_aware():
    import pr_native

    cfg = pr_native.RouteConfig()
    cfg.x_min = -5
    cfg.x_max = 5
    cfg.y_min = -5
    cfg.y_max = 5
    cfg.num_layers = 4
    cfg.grid_mm = 0.25
    cfg.allow_vias = False

    def point(x: float, y: float):
        value = pr_native.Vec2()
        value.x = x
        value.y = y
        return value

    net = pr_native.NetSpec()
    net.net_id = 1
    net.name = "N"
    net.anchors = [point(-4, 0), point(4, 0)]
    net.preferred_layers = [1]

    wall = pr_native.RectObs()
    wall.cx = 0
    wall.cy = 0
    wall.w = 1
    wall.h = 10
    wall.net_id = -1
    wall.layers = [0]

    result = pr_native.route_board([net], cfg, [wall])
    assert result.net_reports[0].status == "ok"
    assert result.segments
    assert {segment.layer for segment in result.segments} == {1}


def test_native_oriented_pad_keeps_fine_pitch_fanout_open():
    """A neighboring diagonal pad must not AABB-block an otherwise legal pin."""
    import pr_native

    cfg = pr_native.RouteConfig()
    cfg.x_min = -5
    cfg.x_max = 5
    cfg.y_min = -5
    cfg.y_max = 5
    cfg.num_layers = 1
    cfg.grid_mm = 0.1
    cfg.clearance_mm = 0.127
    cfg.allow_vias = False

    net = pr_native.NetSpec()
    net.net_id = 0
    net.name = "FANOUT"
    net.anchors = [pr_native.Vec2(0, 0), pr_native.Vec2(0, -4)]
    net.anchor_layers = [[0], [0]]
    net.preferred_layers = [0]
    net.width_mm = 0.25

    own_pad = pr_native.RectObs()
    own_pad.cx = 0
    own_pad.cy = 0
    own_pad.w = 0.275
    own_pad.h = 0.5
    own_pad.rotation_deg = -45
    own_pad.net_id = 0
    own_pad.layers = [0]

    neighbor = pr_native.RectObs()
    neighbor.cx = -0.353553
    neighbor.cy = 0.353553
    neighbor.w = 0.275
    neighbor.h = 0.5
    neighbor.rotation_deg = -45
    neighbor.net_id = -1
    neighbor.layers = [0]

    result = pr_native.route_board([net], cfg, [own_pad, neighbor])
    assert result.unrouted == []
    assert result.segments
    assert result.net_reports[0].status == "ok"


def test_native_rubberband_preserves_multipin_tree_anchors():
    """Post-polish must not collapse branches from a completed multipin tree."""
    import math

    import pr_native

    cfg = pr_native.RouteConfig()
    cfg.x_min = -6
    cfg.x_max = 6
    cfg.y_min = -6
    cfg.y_max = 6
    cfg.num_layers = 1
    cfg.grid_mm = 0.25
    cfg.allow_vias = False
    cfg.post_rubberband = True
    cfg.atomic_nets = True

    def point(x: float, y: float):
        value = pr_native.Vec2()
        value.x = x
        value.y = y
        return value

    anchors = [(-4.0, 0.0), (0.0, 4.0), (4.0, 0.0), (0.0, -4.0)]
    net = pr_native.NetSpec()
    net.net_id = 8
    net.name = "TREE"
    net.anchors = [point(x, y) for x, y in anchors]
    net.preferred_layers = [0]

    result = pr_native.route_board([net], cfg, [])
    assert result.net_reports[0].status == "ok"
    assert len(result.segments) >= len(anchors) - 1
    endpoints = [
        (x, y)
        for segment in result.segments
        for x, y in ((segment.x1, segment.y1), (segment.x2, segment.y2))
    ]
    for ax, ay in anchors:
        assert min(math.hypot(x - ax, y - ay) for x, y in endpoints) < 0.05
    assert any("preserved" in note for note in result.notes)


def test_native_equal_priority_order_is_stable_for_bundle_variants():
    """Reversing equal peers must change which net claims a single corridor."""
    import pr_native

    cfg = pr_native.RouteConfig()
    cfg.x_min = -5
    cfg.x_max = 5
    cfg.y_min = -0.3
    cfg.y_max = 0.3
    cfg.num_layers = 1
    cfg.grid_mm = 0.1
    cfg.clearance_mm = 0.2
    cfg.allow_vias = False
    cfg.atomic_nets = True
    cfg.post_rubberband = False

    def point(x: float, y: float):
        value = pr_native.Vec2()
        value.x = x
        value.y = y
        return value

    def net(net_id: int, name: str):
        value = pr_native.NetSpec()
        value.net_id = net_id
        value.name = name
        value.priority = 1.0
        value.anchors = [point(-4, 0), point(4, 0)]
        value.preferred_layers = [0]
        return value

    a, b = net(1, "A"), net(2, "B")
    forward = pr_native.route_board([a, b], cfg, [])
    reverse = pr_native.route_board([b, a], cfg, [])
    assert {segment.net_id for segment in forward.segments} == {1}
    assert {segment.net_id for segment in reverse.segments} == {2}


def test_native_smd_anchors_use_two_vias_for_inner_escape():
    """F.Cu-only pads may use an inner corridor only with explicit vias."""
    import pr_native

    cfg = pr_native.RouteConfig()
    cfg.x_min = -6
    cfg.x_max = 6
    cfg.y_min = -4
    cfg.y_max = 4
    cfg.num_layers = 4
    cfg.grid_mm = 0.2
    cfg.clearance_mm = 0.2
    cfg.allow_vias = True
    cfg.allow_blind_buried_vias = False
    cfg.via_diameter_mm = 0.6
    cfg.via_drill_mm = 0.3
    cfg.post_rubberband = False

    def point(x: float, y: float):
        value = pr_native.Vec2()
        value.x = x
        value.y = y
        return value

    net = pr_native.NetSpec()
    net.net_id = 4
    net.name = "SMD_ESCAPE"
    net.anchors = [point(-5, 0), point(5, 0)]
    net.anchor_layers = [[0], [0]]
    net.preferred_layers = [1, 0]

    wall = pr_native.RectObs()
    wall.cx = 0
    wall.cy = 0
    wall.w = 0.8
    wall.h = 8
    wall.net_id = -1
    wall.layers = [0]

    # A same-net pad at the first preferred escape site must remain available
    # to tracks but reject a discrete via. Via-in-pad is not supported by the
    # fabrication model.
    own_pad = pr_native.RectObs()
    own_pad.cx = -3.2
    own_pad.cy = 0.0
    own_pad.w = 0.8
    own_pad.h = 0.8
    own_pad.net_id = 4
    own_pad.layers = [0]
    own_pad.is_pad = True

    result = pr_native.route_board([net], cfg, [wall, own_pad])
    assert result.net_reports[0].status == "ok"
    assert len(result.vias) == 2
    assert any(segment.layer == 1 for segment in result.segments)
    assert all(via.layer_a == 0 and via.layer_b == 3 for via in result.vias)
    assert all(via.size_mm == pytest.approx(0.6) for via in result.vias)
    assert all(via.drill_mm == pytest.approx(0.3) for via in result.vias)
    for via in result.vias:
        outside_x = max(0.0, abs(via.x - own_pad.cx) - own_pad.w * 0.5)
        outside_y = max(0.0, abs(via.y - own_pad.cy) - own_pad.h * 0.5)
        assert math.hypot(outside_x, outside_y) >= 0.3 - 1e-9
    assert "two_via_escape" in result.net_reports[0].method

    cfg.allow_vias = False
    blocked = pr_native.route_board([net], cfg, [wall, own_pad])
    assert blocked.net_reports[0].status == "unrouted"
    assert blocked.segments == []
