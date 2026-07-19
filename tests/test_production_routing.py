"""Production-routing preflight and section planning tests."""

from __future__ import annotations

import math
from types import SimpleNamespace

from physics_router.design_rules import default_design_rules
from physics_router.global_router import build_global_route_plan
from physics_router.models import BoardModel, Component, PlacementConfig
from physics_router.pin_access import build_pin_access_plan
from physics_router.router import _pad_polygon_board, _point_polygon_distance


def _crossing_smd_board() -> BoardModel:
    positions = {
        "A": (2.0, 5.0, "H"),
        "B": (18.0, 5.0, "H"),
        "C": (10.0, 1.5, "V"),
        "D": (10.0, 8.5, "V"),
    }
    components = {
        ref: Component(
            ref=ref,
            x_mm=x,
            y_mm=y,
            width_mm=1.0,
            height_mm=1.0,
            pads=[
                {
                    "num": "1",
                    "net": net,
                    "x": 0.0,
                    "y": 0.0,
                    "w": 0.5,
                    "h": 0.5,
                    "shape": "rect",
                    "layers": ["F.Cu"],
                }
            ],
        )
        for ref, (x, y, net) in positions.items()
    }
    return BoardModel(
        width_mm=20.0,
        height_mm=10.0,
        copper_layers=["F.Cu", "In1.Cu", "B.Cu"],
        components=components,
        nets={"H": [("A", "1"), ("B", "1")], "V": [("C", "1"), ("D", "1")]},
        outline=[
            {
                "kind": "poly",
                "pts": [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)],
                "closed": True,
                "layer": "Edge.Cuts",
            }
        ],
    )


def test_pin_access_sites_are_offset_from_every_pad_and_inside_outline() -> None:
    board = _crossing_smd_board()
    rules = default_design_rules().model_copy(update={"copper_layers": board.copper_layers})
    access = build_pin_access_plan(board, rules)

    assert access.metrics["tested_smd_anchors"] == 4
    assert access.metrics["inner_reachable_anchors"] == 4
    assert access.metrics["candidate_sites"] > 0
    via_radius = 0.5 * rules.constraints.min_via_diameter_mm
    edge_keepout = via_radius + rules.constraints.min_copper_edge_clearance_mm
    for pad_accesses in access.by_net.values():
        for pad_access in pad_accesses:
            assert pad_access.candidates
            for site in pad_access.candidates:
                assert edge_keepout <= site.x <= board.width_mm - edge_keepout
                assert edge_keepout <= site.y <= board.height_mm - edge_keepout
                for component in board.components.values():
                    polygon = _pad_polygon_board(component, component.pads[0])
                    required = via_radius
                    if component.pads[0]["net"] != site.net:
                        required += rules.constraints.min_clearance_mm
                    assert _point_polygon_distance((site.x, site.y), polygon) >= required - 1e-8


def test_global_sections_align_with_topology_and_only_use_legal_access() -> None:
    board = _crossing_smd_board()
    rules = default_design_rules().model_copy(update={"copper_layers": board.copper_layers})
    access = build_pin_access_plan(board, rules)
    route_plan = build_global_route_plan(
        board,
        PlacementConfig(),
        rules,
        access,
        cell_mm=0.5,
        max_iterations=4,
    )

    assert route_plan.metrics["sections"] == 2
    assert route_plan.metrics["iterations"] >= 1
    for net in board.nets:
        edges = route_plan.topology_edges(net)
        layers = route_plan.topology_edge_layers(net)
        assert len(edges) == len(layers) == 1
        vertices = route_plan.topology.hyperedges[net].vertices
        for endpoint in edges[0]:
            if layers[0] not in vertices[endpoint].layers:
                assert route_plan.access_sites_for(net, endpoint)


def test_pin_access_candidates_are_deterministic() -> None:
    board = _crossing_smd_board()
    rules = default_design_rules().model_copy(update={"copper_layers": board.copper_layers})
    first = build_pin_access_plan(board, rules).to_dict()
    second = build_pin_access_plan(board, rules).to_dict()
    assert first == second
    assert math.isfinite(first["pads"]["H"][0]["candidates"][0]["score"])


def test_native_bridge_exports_and_consumes_production_plan() -> None:
    from physics_router.native_bridge import available, route_board_native

    assert available()

    board = _crossing_smd_board()
    rules = default_design_rules().model_copy(update={"copper_layers": board.copper_layers})
    access = build_pin_access_plan(board, rules)
    route_plan = build_global_route_plan(board, None, rules, access)

    raw = route_board_native(
        board,
        None,
        grid_mm=0.15,
        use_gpu=False,
        routing_plan=route_plan,
    )

    assert raw is not None
    exported = raw["quality"]["production_route_plan"]
    assert exported["algorithm"] == "capacity_pathfinder+section_layer_assignment"
    assert exported["pin_access"]["metrics"]["candidate_sites"] > 0
    assert all(
        len(exported["sections"][net]) == len(route_plan.topology_edges(net))
        for net in board.nets
    )


def test_hybrid_route_has_strict_manufacturing_success_gate() -> None:
    from physics_router.hybrid_route import hybrid_route

    board = _crossing_smd_board()
    rules = default_design_rules().model_copy(update={"copper_layers": board.copper_layers})
    result = hybrid_route(board, None, rules)

    assert result.quality["manufacturing_gate"] == {
        "passed": True,
        "status": "native_candidate",
        "complete_nets": 2,
        "required_nets": 2,
        "unrouted_nets": [],
        "native_drc_violations": 0,
        "kicad_drc_required": True,
    }


def test_native_detailed_router_uses_reserved_two_via_access_first() -> None:
    from physics_router.native_bridge import available, route_board_native

    assert available()
    board = _crossing_smd_board()
    reserved = {0: [(2.0, 4.0)], 1: [(18.0, 4.0)]}

    class ReservedPlan:
        pin_access = SimpleNamespace(via_diameter_mm=0.6, via_drill_mm=0.3)

        @staticmethod
        def topology_edges(_net: str) -> list[tuple[int, int]]:
            return [(0, 1)]

        @staticmethod
        def topology_edge_layers(_net: str) -> list[str]:
            return ["In1.Cu"]

        @staticmethod
        def access_sites_for(_net: str, anchor_index: int) -> list[tuple[float, float]]:
            return reserved[anchor_index]

        @staticmethod
        def preferred_layers(_net: str) -> list[str]:
            return ["In1.Cu", "F.Cu", "B.Cu"]

        @staticmethod
        def to_dict() -> dict[str, str]:
            return {"algorithm": "test_reserved_access"}

    raw = route_board_native(
        board,
        None,
        grid_mm=0.15,
        use_gpu=False,
        net_order=["H"],
        exclusive_nets=True,
        routing_plan=ReservedPlan(),
    )

    assert raw is not None and raw["unrouted_nets"] == []
    assert len(raw["vias"]) == 2
    actual = sorted((via["x"], via["y"]) for via in raw["vias"])
    expected = sorted([(2.0, 4.0), (18.0, 4.0)])
    assert all(
        math.hypot(got_x - want_x, got_y - want_y) <= 0.15
        for (got_x, got_y), (want_x, want_y) in zip(actual, expected, strict=True)
    )
    assert all("Reserved legal SMD access" in via["reason"] for via in raw["vias"])
