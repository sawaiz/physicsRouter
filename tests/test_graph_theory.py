"""Graph-theoretic topology planning and native consumption tests."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

from physics_router.graph_theory import (
    GraphVertex,
    NetHyperedge,
    annular_spanning_tree,
    analyze_route_graph,
    build_board_hypergraph,
    crossing_conflict_graph,
    dsatur_layer_coloring,
    minimum_spanning_tree,
    plan_graph_topology,
)
from physics_router.models import BoardModel, Component
from physics_router.router import RouteResult, RouteSegment, Via


def _crossing_board() -> BoardModel:
    components: dict[str, Component] = {}
    nets = {
        "ROW": [("A", "1"), ("B", "1")],
        "COL": [("C", "1"), ("D", "1")],
        "BUS": [("A", "2"), ("C", "2"), ("E", "1")],
    }
    positions = {
        "A": (1.0, 1.0),
        "B": (9.0, 9.0),
        "C": (1.0, 9.0),
        "D": (9.0, 1.0),
        "E": (5.0, 8.0),
    }
    for ref, (x, y) in positions.items():
        pads = []
        for net, pins in nets.items():
            for pin_ref, pad in pins:
                if pin_ref == ref:
                    pads.append(
                        {
                            "num": pad,
                            "net": net,
                            "x": 0.0,
                            "y": 0.0,
                            "layers": ["F.Cu"] if ref != "E" else ["*.Cu"],
                        }
                    )
        components[ref] = Component(ref=ref, x_mm=x, y_mm=y, pads=pads)
    return BoardModel(
        width_mm=10.0,
        height_mm=10.0,
        components=components,
        nets=nets,
        copper_layers=["F.Cu", "In1.Cu", "B.Cu"],
    )


def test_hypergraph_preserves_multipin_nets_and_pad_layers() -> None:
    graph = build_board_hypergraph(_crossing_board())
    assert set(graph) == {"ROW", "COL", "BUS"}
    assert len(graph["BUS"].vertices) == 3
    assert graph["ROW"].vertices[0].layers == ("F.Cu",)
    assert graph["BUS"].vertices[-1].layers == ("F.Cu", "In1.Cu", "B.Cu")


def test_crossing_penalty_changes_spanning_tree_when_alternative_exists() -> None:
    vertices = [
        GraphVertex(i, "N", str(i), "1", x, y, ("F.Cu",))
        for i, (x, y) in enumerate(((0.0, 0.0), (2.0, 2.0), (3.0, 0.0)))
    ]
    hyperedge = NetHyperedge(net="N", vertices=vertices)
    foreign = [((1.0, 0.8), (1.0, 1.2))]
    tree = minimum_spanning_tree(
        hyperedge,
        foreign_edges=foreign,
        crossing_penalty_mm=10.0,
    )
    assert len(tree) == len(vertices) - 1
    assert sum(edge.crossing_cost for edge in tree) == 0


def test_annular_topology_preserves_ring_corridor_and_low_degree() -> None:
    vertices = [GraphVertex(0, "RING", "U", "1", 0.0, 0.0, ("F.Cu",))]
    for index in range(8):
        angle = 2.0 * math.pi * index / 8
        vertices.append(
            GraphVertex(
                index + 1,
                "RING",
                f"D{index}",
                "1",
                10.0 * math.cos(angle),
                10.0 * math.sin(angle),
                ("F.Cu",),
            )
        )
    tree = annular_spanning_tree(NetHyperedge("RING", vertices), center=(0.0, 0.0))
    assert tree is not None and len(tree) == len(vertices) - 1
    degree = [0] * len(vertices)
    for edge in tree:
        degree[edge.u] += 1
        degree[edge.v] += 1
    assert max(degree) <= 2


def test_crossing_conflicts_are_colored_onto_distinct_layers() -> None:
    plan = plan_graph_topology(_crossing_board())
    assert plan.conflict_graph["ROW"]["COL"] == 1
    assert plan.layer_assignment["ROW"] != plan.layer_assignment["COL"]
    assert plan.metrics["projected_same_layer_crossings"] == 0


def test_dsatur_degrades_deterministically_when_layers_are_scarce() -> None:
    board = _crossing_board()
    hyperedges = build_board_hypergraph(board, net_names=["ROW", "COL"])
    trees = {name: minimum_spanning_tree(edge) for name, edge in hyperedges.items()}
    conflicts = crossing_conflict_graph(hyperedges, trees)
    colors = dsatur_layer_coloring(conflicts, hyperedges, ["F.Cu"])
    assert colors == {"COL": "F.Cu", "ROW": "F.Cu"}


def test_embedded_route_graph_reports_crossing_components_and_cycle_rank() -> None:
    route = RouteResult(
        segments=[
            RouteSegment(0, 0, 2, 2, net="A"),
            RouteSegment(0, 2, 2, 0, net="B"),
            RouteSegment(3, 0, 4, 0, net="C"),
            RouteSegment(4, 0, 4, 1, net="C"),
            RouteSegment(4, 1, 3, 0, net="C"),
        ]
    )
    metrics = analyze_route_graph(route)
    assert metrics["crossing_number"] == 1
    assert metrics["crossing_conflict_edges"] == 1
    assert metrics["connected_components"] == 3
    assert metrics["cycle_rank"] == 1
    assert metrics["articulation_points"] == 0
    assert metrics["bridges"] == 2
    assert metrics["per_net"]["C"]["cycle_rank"] == 1


def test_route_graph_through_via_connects_touched_inner_layer() -> None:
    result = RouteResult(
        segments=[
            RouteSegment(0, 0, 1, 0, "F.Cu", "N", 0.2),
            RouteSegment(1, 0, 2, 0, "In1.Cu", "N", 0.2),
        ],
        vias=[Via(1, 0, "N", layers=("F.Cu", "B.Cu"))],
    )

    metrics = analyze_route_graph(result)

    assert metrics["per_net"]["N"]["connected_components"] == 1
    assert metrics["per_net"]["N"]["via_edges"] == 2


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "native" / "build", ROOT / "native" / "build" / "Release"):
    if path.is_dir():
        sys.path.insert(0, str(path))


def test_native_router_consumes_advisory_graph_tree() -> None:
    pr_native = pytest.importorskip("pr_native")

    def point(x: float, y: float):
        value = pr_native.Vec2()
        value.x = x
        value.y = y
        return value

    config = pr_native.RouteConfig()
    config.x_min = -1.0
    config.x_max = 11.0
    config.y_min = -1.0
    config.y_max = 11.0
    config.grid_mm = 0.25
    config.num_layers = 2
    config.post_rubberband = False
    net = pr_native.NetSpec()
    net.net_id = 1
    net.name = "TREE"
    net.anchors = [point(0, 0), point(1, 0), point(10, 0)]
    net.anchor_layers = [[0], [0], [0]]
    net.preferred_layers = [0, 1]
    net.topology_edges = [(0, 2), (2, 1)]

    result = pr_native.route_board([net], config, [])
    assert result.unrouted == []
    assert "graph_tree" in result.net_reports[0].method


def test_topology_plan_is_exported_by_native_bridge() -> None:
    pytest.importorskip("pr_native")
    from physics_router.native_bridge import route_board_native

    raw = route_board_native(_crossing_board(), None, grid_mm=0.5, use_gpu=False)
    assert raw is not None
    plan = raw["quality"]["graph_topology_plan"]
    assert plan["planner"] == "hypergraph+crossing_mst+dsatur"
    assert plan["hyperedges"] == 3


def test_topological_guide_route_is_the_graph_plan() -> None:
    from physics_router.router import topological_guide_route

    route = topological_guide_route(_crossing_board())
    assert route.quality["graph_topology_plan"]["hyperedges"] == 3
    assert all(report.method == "graph_crossing_mst+dsatur" for report in route.net_reports)
    layer_by_net = {segment.net: segment.layer for segment in route.segments}
    assert layer_by_net["ROW"] != layer_by_net["COL"]
