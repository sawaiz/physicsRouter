"""Tests for native routing progress window helpers."""

from __future__ import annotations

from types import SimpleNamespace

from physics_router.progress_ui import (
    _Seg,
    _bounds_from_board,
    _pad_points,
    run_with_progress_window,
)


def test_pad_points_from_components():
    board = SimpleNamespace(
        width_mm=20,
        height_mm=10,
        components={
            "U1": SimpleNamespace(
                x_mm=5.0,
                y_mm=3.0,
                rotation_deg=0,
                pads=[{"x": 0.5, "y": -0.5, "net": "NETA"}],
            )
        },
        nets={},
    )
    pts = _pad_points(board)
    assert len(pts) == 1
    x, y, net = pts[0]
    assert abs(x - 5.5) < 1e-6
    assert abs(y - 2.5) < 1e-6
    assert net == "NETA"


def test_pad_points_fallback_to_net_anchors():
    board = SimpleNamespace(
        width_mm=0,
        height_mm=0,
        components={},
        nets={"GND": [(0.0, 0.0), (1.0, 2.0)]},
    )
    pts = _pad_points(board)
    assert len(pts) == 2
    assert pts[0][2] == "GND"


def test_bounds_from_board_with_pads():
    pads = [(0.0, 0.0, "A"), (10.0, 5.0, "B")]
    board = SimpleNamespace(width_mm=100, height_mm=100)
    xmin, xmax, ymin, ymax = _bounds_from_board(board, pads)
    assert xmin < 0
    assert xmax > 10
    assert ymin < 0
    assert ymax > 5


def test_run_with_progress_window_headless():
    calls: list[tuple] = []

    def work(cb):
        if cb:
            cb(1, 2, "stage_a", "running", {"segment_samples": []})
            cb(
                2,
                2,
                "stage_b",
                "ok",
                {
                    "segment_samples": [
                        {
                            "x1": 0,
                            "y1": 0,
                            "x2": 1,
                            "y2": 1,
                            "layer": "F.Cu",
                            "net": "N",
                            "width_mm": 0.2,
                        }
                    ]
                },
            )
            calls.append(True)
        return SimpleNamespace(
            segments=[
                SimpleNamespace(
                    x1=0, y1=0, x2=1, y2=1, layer="F.Cu", net="N", width_mm=0.2
                )
            ],
            via_count=0,
            unrouted_nets=[],
        )

    result = run_with_progress_window(
        work=work,
        title="test",
        board=None,
        headless=True,
    )
    assert result.via_count == 0
    assert len(result.segments) == 1
    # headless passes None callback — work still returns
    assert calls == []


def test_run_with_progress_window_forwards_progress_when_no_tk(monkeypatch):
    """If tkinter import fails, fall through to headless work(None)."""
    import builtins
    import sys

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tkinter" or name.startswith("tkinter."):
            raise ImportError("no tk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # Also clear any cached tkinter
    sys.modules.pop("tkinter", None)
    sys.modules.pop("tkinter.ttk", None)

    seen = {"cb": "unset"}

    def work(cb):
        seen["cb"] = cb
        return "ok"

    out = run_with_progress_window(work=work, title="t", board=None, headless=False)
    assert out == "ok"
    assert seen["cb"] is None


def test_seg_dataclass():
    s = _Seg(0, 0, 1, 1, "B.Cu", "GND", 0.3)
    assert s.layer == "B.Cu"
    assert s.width == 0.3
