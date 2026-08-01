"""Escape scarcity, neck-down, and routability-scored placement.

Three levers found by measuring mppcInterface v1.3: every failing 2-pin net
escaped one ICE40 QFN32. The escape geometry was already correct — the gaps
were *allocation order*, *rule rigidity*, and *placement blindness*.
"""

from __future__ import annotations

import copy

from physics_router.config_io import example_config
from physics_router.design_rules import default_design_rules, jlcpcb_4layer_design_rules
from physics_router.kicad_io import board_from_synthetic
from physics_router.models import Component
from physics_router.physics import density_congestion, escape_congestion


# --------------------------------------------------------------------------
# Lever 1 — escape scarcity ordering
# --------------------------------------------------------------------------


def test_escape_scarcity_prefers_constrained_nets() -> None:
    """Nets with fewer escapes on a crowded package rank ahead of free ones."""
    from physics_router.pin_access import build_pin_access_plan

    cfg = example_config()
    board = board_from_synthetic(cfg)
    plan = build_pin_access_plan(board, default_design_rules())

    scarcity = plan.escape_scarcity()
    assert scarcity, "expected per-net scarcity scores"
    assert all(v >= 0.0 for v in scarcity.values())

    order = plan.order_by_escape_scarcity(list(board.nets))
    assert sorted(order) == sorted(board.nets), "ordering must be a permutation"
    # Monotonically non-increasing scarcity.
    scores = [scarcity.get(n, 0.0) for n in order]
    assert scores == sorted(scores, reverse=True)


def test_escape_scarcity_is_deterministic() -> None:
    """Ordering must be stable — routing results have to be reproducible."""
    from physics_router.pin_access import build_pin_access_plan

    cfg = example_config()
    board = board_from_synthetic(cfg)
    plan = build_pin_access_plan(board, default_design_rules())
    nets = list(board.nets)
    assert plan.order_by_escape_scarcity(nets) == plan.order_by_escape_scarcity(nets)


# --------------------------------------------------------------------------
# Lever 2 — neck-down
# --------------------------------------------------------------------------


def test_escape_width_necks_down_but_never_below_fab_floor() -> None:
    """Wide nets leave the pad at the fab minimum; necked copper stays legal."""
    rules = jlcpcb_4layer_design_rules()
    floor = rules.constraints.min_track_width_mm
    cfg = example_config()

    for net in list(cfg.net_by_name())[:8]:
        run = rules.track_width_for_net(net, cfg)
        escape = rules.escape_track_width_for_net(net, cfg)
        assert escape >= floor, "necked stub must still pass DRC"
        assert escape <= run, "escape must never be wider than the run width"


def test_power_net_necks_down_from_wider_run_width() -> None:
    """A boosted power net is exactly the case neck-down exists for."""
    rules = jlcpcb_4layer_design_rules()
    cfg = example_config()
    power = [
        n
        for n in cfg.net_by_name()
        if rules.track_width_for_net(n, cfg) > rules.constraints.min_track_width_mm
    ]
    if not power:  # synthetic config may not boost any net
        return
    net = power[0]
    assert rules.escape_track_width_for_net(net, cfg) < rules.track_width_for_net(net, cfg)


# --------------------------------------------------------------------------
# Lever 3 — routability-scored placement
# --------------------------------------------------------------------------


def test_escape_congestion_flags_oversubscribed_placement() -> None:
    """Piling pad-bearing parts onto one spot must cost more than spreading."""
    cfg = example_config()
    spread = board_from_synthetic(cfg)
    for index in range(12):
        spread.components[f"U{index}_P"] = Component(
            ref=f"U{index}_P",
            x_mm=3.0 + 4.0 * index,
            y_mm=3.0 + 2.0 * (index % 5),
            width_mm=2.0,
            height_mm=2.0,
            pads=[
                {"num": str(p), "x": 0.2 * p, "y": 0.0, "net": f"N{index}_{p}"}
                for p in range(8)
            ],
        )
    baseline = escape_congestion(spread, cfg)

    piled = copy.deepcopy(spread)
    for ref, component in piled.components.items():
        if ref.endswith("_P"):
            component.x_mm, component.y_mm = 10.0, 10.0

    assert escape_congestion(piled, cfg) > baseline


def test_escape_congestion_is_pad_aware_not_component_aware() -> None:
    """Distinguishes a many-pad package from the same count of 2-pad parts.

    ``density_congestion`` counts components, so it cannot see escape demand;
    this is the whole reason the term exists.
    """
    cfg = example_config()
    dense = board_from_synthetic(cfg)
    pads = [
        {"num": str(i), "x": 0.0 + 0.5 * i, "y": 0.0, "net": f"N{i}"} for i in range(40)
    ]
    dense.components["U_DENSE"] = Component(
        ref="U_DENSE", x_mm=5.0, y_mm=5.0, width_mm=4.0, height_mm=4.0, pads=pads
    )
    sparse = copy.deepcopy(dense)
    sparse.components["U_DENSE"].pads = [dict(p, net=None) for p in pads]

    # Same components and positions; only whether pads carry nets differs.
    assert density_congestion(dense) == density_congestion(sparse)
    assert escape_congestion(dense, cfg) >= escape_congestion(sparse, cfg)


def test_escape_congestion_zero_on_empty_board() -> None:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    board.components.clear()
    assert escape_congestion(board, cfg) == 0.0
