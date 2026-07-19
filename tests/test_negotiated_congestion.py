"""PathFinder history, resource ownership, and conflict-directed legalization."""

from __future__ import annotations

from physics_router.design_rules import default_design_rules
from physics_router.hybrid_route import classify_board
from physics_router.models import BoardModel, Component, NetLabel, PlacementConfig
from physics_router.negotiated_congestion import (
    _bump_history,
    _legalize,
    negotiated_congestion_route,
    route_resource_owners,
)
from physics_router.router import RouteResult, RouteSegment
from physics_router.topology import CongestionMap


def test_resource_owners_detect_cross_net_overuse() -> None:
    route = RouteResult(
        segments=[
            RouteSegment(0, 1, 10, 1, "F.Cu", "A", 0.2),
            RouteSegment(5, -4, 5, 6, "F.Cu", "B", 0.2),
        ]
    )

    owners = route_resource_owners(
        route,
        cell_mm=0.25,
        clearance_mm=0.2,
        layers=["F.Cu"],
    )

    assert any(value == {"A", "B"} for value in owners.values())


def test_history_is_added_only_to_overused_and_marker_cells() -> None:
    congestion = CongestionMap(cell_mm=0.5, historical_boost=1.0)
    owners = {
        (0, 0, "F.Cu"): {"A"},
        (2, 2, "F.Cu"): {"A", "B"},
    }
    drc = {
        "items": [
            {
                "kind": "spacing",
                "net_a": "A",
                "net_b": "B",
                "layer": "F.Cu",
                "x": 3.2,
                "y": 1.2,
            }
        ]
    }

    _bump_history(congestion, owners, drc)

    assert (0, 0, "F.Cu") not in congestion.historical
    assert congestion.historical[(2, 2, "F.Cu")] == 1.0
    assert congestion.cost(3.2, 1.2, "F.Cu") > 0


def test_conflict_legalizer_rips_lower_priority_net() -> None:
    board = BoardModel(
        width_mm=10,
        height_mm=10,
        copper_layers=["F.Cu"],
        nets={"KEEP": [("A", "1"), ("B", "1")], "RIP": [("C", "1"), ("D", "1")]},
    )
    config = PlacementConfig(
        nets=[
            NetLabel(name="KEEP", weight=5.0, critical=True),
            NetLabel(name="RIP", weight=1.0),
        ]
    )
    route = RouteResult(
        segments=[
            RouteSegment(0, 5, 10, 5, "F.Cu", "KEEP", 0.2),
            RouteSegment(5, 0, 5, 10, "F.Cu", "RIP", 0.2),
        ]
    )

    legal, victims = _legalize(route, board, config, clearance_mm=0.2)

    assert victims == ["RIP"]
    assert {segment.net for segment in legal.segments} == {"KEEP"}


def test_board_wide_negotiation_resolves_crossing_candidates() -> None:
    components = {
        "A": Component(ref="A", x_mm=1, y_mm=5, pads=[{"num": "1", "net": "H", "w": 0.4, "h": 0.4, "layers": ["F.Cu"]}]),
        "B": Component(ref="B", x_mm=9, y_mm=5, pads=[{"num": "1", "net": "H", "w": 0.4, "h": 0.4, "layers": ["F.Cu"]}]),
        "C": Component(ref="C", x_mm=5, y_mm=1, pads=[{"num": "1", "net": "V", "w": 0.4, "h": 0.4, "layers": ["F.Cu"]}]),
        "D": Component(ref="D", x_mm=5, y_mm=9, pads=[{"num": "1", "net": "V", "w": 0.4, "h": 0.4, "layers": ["F.Cu"]}]),
    }
    rules = default_design_rules()
    board = BoardModel(
        width_mm=10,
        height_mm=10,
        copper_layers=["F.Cu"],
        components=components,
        nets={"H": [("A", "1"), ("B", "1")], "V": [("C", "1"), ("D", "1")]},
        design_rules=rules.summary(),
    )
    config = PlacementConfig(nets=[NetLabel(name="H"), NetLabel(name="V")])

    result = negotiated_congestion_route(
        board,
        config,
        rules,
        classify_board(board, config, rules),
        RouteResult(),
        clearance_mm=0.2,
        max_iterations=3,
        workers=1,
    )

    assert result.unrouted_nets == []
    assert {report.net for report in result.net_reports if report.status == "ok"} == {"H", "V"}
    assert result.quality["negotiated_congestion"]["complete_nets"] == 2
