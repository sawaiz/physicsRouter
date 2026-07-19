"""Capacity-aware board-level section and layer planning.

This is the global stage between graph topology and exact copper.  It assigns
each tree section to a copper layer while negotiating coarse corridor capacity.
The detailed C++ router remains responsible for exact obstacle avoidance, but
it no longer receives only one decorative color for an entire multipin net.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from physics_router.design_rules import DesignRules
from physics_router.graph_theory import GraphEdge, GraphTopologyPlan, plan_graph_topology
from physics_router.models import BoardModel, PlacementConfig
from physics_router.pin_access import PinAccessPlan

# capacity_mesh is optional — imported lazily in build_global_route_plan


ResourceKey = tuple[int, int, str]


@dataclass(frozen=True)
class SectionAssignment:
    net: str
    edge_index: int
    u: int
    v: int
    layer: str
    cells: tuple[ResourceKey, ...]
    via_count: int
    cost: float


@dataclass
class GlobalRoutePlan:
    topology: GraphTopologyPlan
    pin_access: PinAccessPlan
    sections: dict[str, list[SectionAssignment]]
    copper_layers: list[str]
    metrics: dict[str, Any] = field(default_factory=dict)

    def topology_edges(self, net: str) -> list[tuple[int, int]]:
        return self.topology.topology_edges(net)

    def topology_edge_layers(self, net: str) -> list[str]:
        values = self.sections.get(net) or []
        by_index = {value.edge_index: value.layer for value in values}
        return [
            by_index.get(index, self.topology.layer_assignment.get(net, self.copper_layers[0]))
            for index, _edge in enumerate(self.topology.trees.get(net, []))
        ]

    def preferred_layers(self, net: str) -> list[str]:
        counts = Counter(value.layer for value in self.sections.get(net) or [])
        return sorted(
            self.copper_layers,
            key=lambda layer: (
                -counts.get(layer, 0),
                self.copper_layers.index(layer),
            ),
        )

    def access_sites_for(self, net: str, anchor_index: int) -> list[tuple[float, float]]:
        return self.pin_access.sites_for(net, anchor_index)

    def net_order(self, config: PlacementConfig | None = None) -> list[str]:
        def key(net: str) -> tuple:
            anchors = self.pin_access.by_net.get(net) or []
            constrained = sum(
                1
                for value in anchors
                if len(value.layers) < len(self.copper_layers) and not value.candidates
            )
            candidates = sum(len(value.candidates) for value in anchors)
            priority = config.weight_for_net(net) if config is not None else 1.0
            return (-constrained, candidates, -priority, -len(anchors), net)

        return sorted(self.topology.hyperedges, key=key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": "capacity_pathfinder+section_layer_assignment",
            "topology": self.topology.to_dict(),
            "pin_access": self.pin_access.to_dict(),
            "metrics": dict(self.metrics),
            "sections": {
                net: [
                    {
                        "edge_index": value.edge_index,
                        "u": value.u,
                        "v": value.v,
                        "layer": value.layer,
                        "cells": len(value.cells),
                        "via_count": value.via_count,
                        "cost": round(value.cost, 4),
                    }
                    for value in values
                ]
                for net, values in self.sections.items()
            },
        }


def _section_cells(
    start: tuple[float, float],
    goal: tuple[float, float],
    layer: str,
    cell_mm: float,
) -> tuple[ResourceKey, ...]:
    length = math.hypot(goal[0] - start[0], goal[1] - start[1])
    samples = max(1, int(math.ceil(length / max(0.1, 0.45 * cell_mm))))
    result: list[ResourceKey] = []
    for index in range(samples + 1):
        ratio = index / samples
        x = start[0] + (goal[0] - start[0]) * ratio
        y = start[1] + (goal[1] - start[1]) * ratio
        key = (int(math.floor(x / cell_mm)), int(math.floor(y / cell_mm)), layer)
        if not result or result[-1] != key:
            result.append(key)
    return tuple(result)


def build_global_route_plan(
    board: BoardModel,
    config: PlacementConfig | None,
    rules: DesignRules,
    pin_access: PinAccessPlan,
    *,
    cell_mm: float | None = None,
    max_iterations: int = 12,
    capacity_mesh: Any | None = None,
    effort: float = 0.55,
) -> GlobalRoutePlan:
    """Negotiate coarse capacity and assign every topology section a layer.

    When *capacity_mesh* is provided (tscircuit-style hierarchical mesh), leaf
    size informs *cell_mm* and mesh path lengths bias section cost.
    """
    layers = list(board.copper_layers or ["F.Cu", "B.Cu"])
    topology = plan_graph_topology(board, config, layers=layers)
    if capacity_mesh is None and effort > 0:
        try:
            from physics_router.capacity_mesh import build_capacity_mesh

            targets = []
            for net, he in topology.hyperedges.items():
                for v in he.vertices:
                    targets.append((v.x, v.y, net))
            capacity_mesh = build_capacity_mesh(
                board, rules, effort=effort, targets=targets
            )
        except Exception:
            capacity_mesh = None
    if capacity_mesh is not None and cell_mm is None and getattr(capacity_mesh, "nodes", None):
        avg_w = sum(n.width for n in capacity_mesh.nodes) / max(1, len(capacity_mesh.nodes))
        cell_mm = max(0.45, 0.55 * avg_w)
    cell = max(
        0.45,
        float(cell_mm or max(0.65, 4.0 * rules.constraints.min_clearance_mm)),
    )
    pitch = rules.constraints.min_track_width_mm + rules.constraints.min_clearance_mm
    capacity = max(1, int(math.floor(cell / max(0.1, pitch))))
    history: dict[ResourceKey, float] = defaultdict(float)
    assignments: dict[tuple[str, int], SectionAssignment] = {}

    def legal_layer(net: str, edge: GraphEdge, layer: str) -> tuple[bool, int]:
        vertices = topology.hyperedges[net].vertices
        vias = 0
        for endpoint in (edge.u, edge.v):
            vertex = vertices[endpoint]
            if layer in vertex.layers:
                continue
            if not pin_access.has_inner_access(net, endpoint):
                return False, 0
            vias += 1
        return True, vias

    def make_assignment(
        net: str,
        edge_index: int,
        edge: GraphEdge,
        layer: str,
        occupancy: dict[ResourceKey, int],
    ) -> SectionAssignment | None:
        allowed, vias = legal_layer(net, edge, layer)
        if not allowed:
            return None
        vertices = topology.hyperedges[net].vertices
        start = vertices[edge.u]
        goal = vertices[edge.v]
        cells = _section_cells((start.x, start.y), (goal.x, goal.y), layer, cell)
        overflow = sum(max(0, occupancy.get(key, 0) + 1 - capacity) for key in cells)
        present = sum(occupancy.get(key, 0) for key in cells)
        historical = sum(history.get(key, 0.0) for key in cells)
        baseline = topology.layer_assignment.get(net)
        layer_change = 0.0 if layer == baseline else 0.35
        mesh_bias = 0.0
        if capacity_mesh is not None:
            try:
                from physics_router.capacity_mesh import path_through_mesh

                path = path_through_mesh(
                    capacity_mesh, (start.x, start.y), (goal.x, goal.y)
                )
                if not path:
                    mesh_bias = 8.0  # no mesh corridor — discourage
                else:
                    mesh_bias = 0.15 * max(0, len(path) - 1)
            except Exception:
                mesh_bias = 0.0
        cost = edge.length_mm + 5.0 * vias + 2.5 * present + 18.0 * overflow
        cost += historical + layer_change + mesh_bias
        return SectionAssignment(
            net=net,
            edge_index=edge_index,
            u=edge.u,
            v=edge.v,
            layer=layer,
            cells=cells,
            via_count=vias,
            cost=cost,
        )

    section_keys = [
        (net, index, edge)
        for net in topology.trees
        for index, edge in enumerate(topology.trees[net])
    ]
    section_keys.sort(
        key=lambda value: (
            -(config.weight_for_net(value[0]) if config is not None else 1.0),
            -value[2].crossing_cost,
            -value[2].length_mm,
            value[0],
            value[1],
        )
    )

    overflow_history: list[int] = []
    for iteration in range(max(1, max_iterations)):
        occupancy: dict[ResourceKey, int] = defaultdict(int)
        next_assignments: dict[tuple[str, int], SectionAssignment] = {}
        for net, edge_index, edge in section_keys:
            candidates = [
                value
                for layer in layers
                if (
                    value := make_assignment(net, edge_index, edge, layer, occupancy)
                )
                is not None
            ]
            if not candidates:
                # This should be rare: the exposed pad layers always provide at
                # least one outer-layer choice. Retain a diagnostic assignment.
                fallback = topology.hyperedges[net].vertices[edge.u].layers[0]
                candidates = [
                    make_assignment(net, edge_index, edge, fallback, occupancy)
                ]
            selected = min(
                (value for value in candidates if value is not None),
                key=lambda value: (value.cost, layers.index(value.layer)),
            )
            next_assignments[(net, edge_index)] = selected
            for resource in selected.cells:
                occupancy[resource] += 1

        overflow_cells = {
            resource: demand - capacity
            for resource, demand in occupancy.items()
            if demand > capacity
        }
        overflow = sum(overflow_cells.values())
        overflow_history.append(overflow)
        assignments = next_assignments
        if overflow == 0:
            break
        for resource, amount in overflow_cells.items():
            history[resource] += 4.0 * amount

    sections: dict[str, list[SectionAssignment]] = defaultdict(list)
    for (net, _edge_index), assignment in assignments.items():
        sections[net].append(assignment)
    for values in sections.values():
        values.sort(key=lambda value: value.edge_index)

    return GlobalRoutePlan(
        topology=topology,
        pin_access=pin_access,
        sections=dict(sections),
        copper_layers=layers,
        metrics={
            "cell_mm": cell,
            "cell_capacity": capacity,
            "iterations": len(overflow_history),
            "overflow_history": overflow_history,
            "final_overflow": overflow_history[-1] if overflow_history else 0,
            "historical_cells": len(history),
            "sections": len(section_keys),
            "planned_vias": sum(value.via_count for value in assignments.values()),
            "layer_sections": dict(
                sorted(Counter(value.layer for value in assignments.values()).items())
            ),
            "capacity_mesh": capacity_mesh.to_dict() if capacity_mesh is not None else None,
            "effort": effort,
        },
    )
