"""Overflow-aware Steiner trees and cut-capacity certificates."""

from __future__ import annotations

import math

from physics_router.graph_theory import (
    GraphVertex,
    NetHyperedge,
    cut_capacity_preflight,
    minimum_spanning_tree,
    occupancy_from_trees,
    overflow_aware_steiner_tree,
    plan_graph_topology,
)
from physics_router.models import BoardModel, Component
from physics_router.routing_strategies import pre_route_analysis
from physics_router.design_rules import default_design_rules


def _hyperedge_square() -> NetHyperedge:
    """Four pins at square corners — Steiner can improve vs pure MST under overflow."""
    verts = [
        GraphVertex(0, "N", "A", "1", 0.0, 0.0, ("F.Cu",)),
        GraphVertex(1, "N", "B", "1", 10.0, 0.0, ("F.Cu",)),
        GraphVertex(2, "N", "C", "1", 10.0, 10.0, ("F.Cu",)),
        GraphVertex(3, "N", "D", "1", 0.0, 10.0, ("F.Cu",)),
    ]
    return NetHyperedge(net="N", vertices=verts)


def test_overflow_steiner_connects_all_terminals() -> None:
    he = _hyperedge_square()
    tree = overflow_aware_steiner_tree(he, overflow_penalty_mm=0.0)
    assert len(tree) == 3  # n-1
    verts = {0, 1, 2, 3}
    for e in tree:
        assert e.u in verts and e.v in verts


def test_overflow_steiner_avoids_congested_cells() -> None:
    """Heavy occupancy on the short diagonal should prefer other topology."""
    he = _hyperedge_square()
    # Paint center cells as heavily used
    occ: dict[tuple[int, int], float] = {}
    for ix in range(3, 8):
        for iy in range(3, 8):
            occ[(ix, iy)] = 20.0
    tree = overflow_aware_steiner_tree(
        he,
        occupancy=occ,
        cell_mm=1.0,
        overflow_penalty_mm=5.0,
    )
    assert len(tree) == 3
    # Pure MST also works; steiner must not be worse than MST under same costs
    mst = minimum_spanning_tree(
        he, occupancy=occ, cell_mm=1.0, overflow_penalty_mm=5.0
    )
    st_w = sum(e.weight for e in tree)
    mst_w = sum(e.weight for e in mst)
    assert st_w <= mst_w * 1.05 + 1e-6


def test_occupancy_from_trees_marks_cells() -> None:
    he = _hyperedge_square()
    mst = minimum_spanning_tree(he)
    occ = occupancy_from_trees({"N": he}, {"N": mst}, cell_mm=1.0)
    assert len(occ) > 0
    assert max(occ.values()) >= 1.0


def test_cut_preflight_detects_forced_crossings() -> None:
    # Board with nets that must cross mid-plane
    components = {
        "L": Component(
            ref="L",
            x_mm=2,
            y_mm=10,
            pads=[{"num": "1", "net": "H", "x": 0, "y": 0, "layers": ["F.Cu"]}],
        ),
        "R": Component(
            ref="R",
            x_mm=18,
            y_mm=10,
            pads=[{"num": "1", "net": "H", "x": 0, "y": 0, "layers": ["F.Cu"]}],
        ),
        "B": Component(
            ref="B",
            x_mm=10,
            y_mm=2,
            pads=[{"num": "1", "net": "V", "x": 0, "y": 0, "layers": ["F.Cu"]}],
        ),
        "T": Component(
            ref="T",
            x_mm=10,
            y_mm=18,
            pads=[{"num": "1", "net": "V", "x": 0, "y": 0, "layers": ["F.Cu"]}],
        ),
    }
    board = BoardModel(
        width_mm=20,
        height_mm=20,
        components=components,
        nets={"H": [("L", "1"), ("R", "1")], "V": [("B", "1"), ("T", "1")]},
        copper_layers=["F.Cu", "B.Cu"],
    )
    # Tiny pitch => low capacity, both nets forced across center cuts
    rep = cut_capacity_preflight(board, track_pitch_mm=5.0, copper_layers=1)
    assert "certificates" in rep
    assert any(c["demand"] >= 1 for c in rep["certificates"])
    # With huge pitch relative to board, capacity is small
    assert rep["worst"] is not None


def test_cut_preflight_saturated_tiny_capacity() -> None:
    components = {}
    nets = {}
    # Many left-right nets
    for i in range(6):
        nets[f"N{i}"] = [(f"L{i}", "1"), (f"R{i}", "1")]
        components[f"L{i}"] = Component(
            ref=f"L{i}",
            x_mm=1,
            y_mm=2 + i * 2,
            pads=[{"num": "1", "net": f"N{i}", "x": 0, "y": 0, "layers": ["F.Cu"]}],
        )
        components[f"R{i}"] = Component(
            ref=f"R{i}",
            x_mm=19,
            y_mm=2 + i * 2,
            pads=[{"num": "1", "net": f"N{i}", "x": 0, "y": 0, "layers": ["F.Cu"]}],
        )
    board = BoardModel(
        width_mm=20,
        height_mm=20,
        components=components,
        nets=nets,
        copper_layers=["F.Cu"],
    )
    rep = cut_capacity_preflight(board, track_pitch_mm=10.0, copper_layers=1)
    # capacity per vertical cut ≈ floor(20/10)*1 = 2; demand = 6
    assert rep["saturated_cuts"] >= 1
    assert rep["feasible_under_model"] is False


def test_plan_graph_topology_reports_steiner_and_cuts() -> None:
    components = {
        "A": Component(
            ref="A",
            x_mm=1,
            y_mm=1,
            pads=[
                {"num": "1", "net": "BUS", "x": 0, "y": 0, "layers": ["F.Cu"]},
                {"num": "2", "net": "SIG", "x": 0.5, "y": 0, "layers": ["F.Cu"]},
            ],
        ),
        "B": Component(
            ref="B",
            x_mm=9,
            y_mm=1,
            pads=[{"num": "1", "net": "BUS", "x": 0, "y": 0, "layers": ["F.Cu"]}],
        ),
        "C": Component(
            ref="C",
            x_mm=9,
            y_mm=9,
            pads=[{"num": "1", "net": "BUS", "x": 0, "y": 0, "layers": ["F.Cu"]}],
        ),
        "D": Component(
            ref="D",
            x_mm=1,
            y_mm=9,
            pads=[
                {"num": "1", "net": "BUS", "x": 0, "y": 0, "layers": ["F.Cu"]},
                {"num": "2", "net": "SIG", "x": 0.5, "y": 0, "layers": ["F.Cu"]},
            ],
        ),
    }
    board = BoardModel(
        width_mm=10,
        height_mm=10,
        components=components,
        nets={
            "BUS": [("A", "1"), ("B", "1"), ("C", "1"), ("D", "1")],
            "SIG": [("A", "2"), ("D", "2")],
        },
        copper_layers=["F.Cu", "In1.Cu", "B.Cu"],
    )
    plan = plan_graph_topology(board, use_overflow_steiner=True, run_cut_preflight=True)
    assert plan.metrics.get("cut_preflight") is not None
    assert "steiner_nets" in plan.metrics
    assert plan.metrics.get("planner", "").startswith("hypergraph")
    assert len(plan.trees["BUS"]) == 3
    assert len(plan.trees["SIG"]) == 1


def test_pre_route_includes_cut_preflight() -> None:
    components = {
        "L": Component(
            ref="L",
            x_mm=1,
            y_mm=5,
            pads=[{"num": "1", "net": "N", "x": 0, "y": 0, "layers": ["F.Cu"]}],
        ),
        "R": Component(
            ref="R",
            x_mm=19,
            y_mm=5,
            pads=[{"num": "1", "net": "N", "x": 0, "y": 0, "layers": ["F.Cu"]}],
        ),
    }
    board = BoardModel(
        width_mm=20,
        height_mm=10,
        components=components,
        nets={"N": [("L", "1"), ("R", "1")]},
        copper_layers=["F.Cu", "B.Cu"],
    )
    report = pre_route_analysis(board, rules=default_design_rules())
    d = report.to_dict()
    assert "cut_preflight" in d
    assert d["cut_preflight"]["algorithm"] == "geometric_cut_capacity_preflight"
