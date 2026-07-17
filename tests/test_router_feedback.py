"""Router quality feedback + center-origin extent (HALO-style)."""

from __future__ import annotations

from physics_router.config_io import example_config
from physics_router.kicad_io import board_from_synthetic
from physics_router.models import BoardModel, Component
from physics_router.router import board_extent, clearance_aware_route, build_obstacle_map


def test_board_extent_center_origin() -> None:
    board = BoardModel(
        width_mm=24,
        height_mm=26,
        components={
            "D1": Component(ref="D1", x_mm=11.0, y_mm=0.0, width_mm=1, height_mm=1),
            "D2": Component(ref="D2", x_mm=-11.0, y_mm=0.0, width_mm=1, height_mm=1),
            "U1": Component(ref="U1", x_mm=0.0, y_mm=0.0, width_mm=4, height_mm=4),
        },
        nets={"CPX-0": [("U1", "1"), ("D1", "1"), ("D2", "1")]},
    )
    x0, x1, y0, y1 = board_extent(board)
    assert x0 < -10 and x1 > 10
    om = build_obstacle_map(board, clearance_mm=0.15)
    assert om.in_bounds(0, 0)
    assert om.in_bounds(10, 0)
    assert om.in_bounds(-10, 0)
    # classic corner origin still works
    corner = BoardModel(
        width_mm=50,
        height_mm=40,
        components={"R1": Component(ref="R1", x_mm=5, y_mm=5, width_mm=1, height_mm=1)},
        nets={},
    )
    a, b, c, d = board_extent(corner)
    assert a <= 0 and b >= 50 and c <= 0 and d >= 40


def test_route_quality_and_net_reports() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    seen: list[str] = []

    def cb(done, total, name, stage, detail):
        seen.append(f"{done}/{total}:{name}:{stage}")

    route = clearance_aware_route(
        board,
        cfg,
        clearance_mm=0.2,
        grid_mm=1.0,
        allow_vias=True,
        soft_fallback=False,
        progress_cb=cb,
    )
    d = route.to_dict()
    assert "quality" in d
    assert "score" in d["quality"]
    assert "grade" in d["quality"]
    assert "net_reports" in d
    assert len(d["net_reports"]) >= 1
    # clearance mode must not invent illegal straight copper
    assert all(
        "straight_fallback" not in (r.get("method") or "")
        for r in d["net_reports"]
    )
    assert len(seen) >= 1
    for r in d["net_reports"]:
        assert r["status"] in ("ok", "soft_violation", "unrouted", "skipped", "partial")


def test_guide_route_feedback() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    route = clearance_aware_route(
        board, cfg, clearance_mm=0.0, allow_vias=False, guide_only=True
    )
    q = route.compute_quality()
    assert q["score"] >= 0
    assert route.total_length_mm > 0
