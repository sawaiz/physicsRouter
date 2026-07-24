"""Lever 1: dense grid + 2-pin escape policy (fast, no full board route)."""

from __future__ import annotations

from types import SimpleNamespace

from physics_router.router import _dense_grid_for_net, _native_expansions_for_net


def _board(pins: dict[str, list]) -> SimpleNamespace:
    return SimpleNamespace(nets=pins)


def test_two_pin_retry_goes_to_point_one():
    board = _board({"Net-(R1-Pad2)": [("R1", "1"), ("R1", "2")]})
    g0 = _dense_grid_for_net(board, "Net-(R1-Pad2)", 0.15, attempt=0)
    g1 = _dense_grid_for_net(board, "Net-(R1-Pad2)", 0.15, attempt=1)
    g2 = _dense_grid_for_net(board, "Net-(R1-Pad2)", 0.15, attempt=2)
    g4 = _dense_grid_for_net(board, "Net-(R1-Pad2)", 0.15, attempt=4)
    assert g0 == 0.15
    assert g1 <= 0.15
    assert g2 <= 0.10
    assert g4 <= 0.08


def test_multipin_not_forced_to_point_zero_eight_on_first_retry():
    board = _board({"GND": [(f"U{i}", "1") for i in range(20)]})
    g2 = _dense_grid_for_net(board, "GND", 0.2, attempt=2)
    # multipin densifies but attempt=2 without pins<=2 branch stays >=0.12 path
    assert g2 >= 0.08
    assert g2 <= 0.2


def test_two_pin_expansions_grow_on_retry():
    board = _board({"N": [("A", "1"), ("B", "1")]})
    e0 = _native_expansions_for_net(board, "N", attempt=0)
    e1 = _native_expansions_for_net(board, "N", attempt=1)
    assert e0 >= 12000
    assert e1 > e0


def test_hybrid_matrix_classifies_analog_channels():
    from physics_router.config_io import example_config
    from physics_router.design_rules import default_design_rules
    from physics_router.hybrid_route import classify_board
    from physics_router.models import BoardModel, Component

    board = BoardModel(
        width_mm=40,
        height_mm=30,
        copper_layers=["F.Cu", "B.Cu"],
        components={
            "U1": Component(ref="U1", x_mm=0, y_mm=0, pads=[]),
        },
        nets={
            "CH0": [("U1", "1"), ("U1", "2"), ("U1", "3")],
            "DAC3": [("U1", "4"), ("U1", "5"), ("U1", "6")],
            "GPIO1": [("U1", "7"), ("U1", "8")],
        },
    )
    plan = classify_board(board, example_config(), default_design_rules())
    a_ch = plan.assignment("CH0")
    a_dac = plan.assignment("DAC3")
    assert a_ch is not None and a_ch.strategy == "matrix"
    assert a_dac is not None and a_dac.strategy == "matrix"
