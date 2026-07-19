"""Graph-theoretic PCB topology planning and route analysis.

The board is represented simultaneously as:

* a **hypergraph** (one hyperedge per multi-pin net),
* a per-net candidate graph used to choose a crossing-aware spanning tree,
* a net conflict graph colored onto copper layers, and
* an embedded copper graph used to report crossings, components, and cycles.

This module deliberately has no NetworkX/SciPy dependency.  The graph sizes in
PCB topology planning are modest enough for deterministic Kruskal + DSATUR,
and keeping the implementation local makes its routing policy inspectable.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

from physics_router.models import BoardModel, PlacementConfig


@dataclass(frozen=True)
class GraphVertex:
    """A physical pad vertex in one net hyperedge."""

    index: int
    net: str
    ref: str
    pad: str
    x: float
    y: float
    layers: tuple[str, ...]


@dataclass(frozen=True)
class GraphEdge:
    """A selected topological connection between two pad vertices."""

    net: str
    u: int
    v: int
    length_mm: float
    crossing_cost: int = 0
    weight: float = 0.0


@dataclass
class NetHyperedge:
    """A multi-pin electrical net represented as a hyperedge."""

    net: str
    vertices: list[GraphVertex]
    priority: float = 1.0


@dataclass
class GraphTopologyPlan:
    """Board-level graph plan consumed by the native geometrizer."""

    hyperedges: dict[str, NetHyperedge]
    trees: dict[str, list[GraphEdge]]
    conflict_graph: dict[str, dict[str, int]]
    layer_assignment: dict[str, str]
    metrics: dict[str, Any] = field(default_factory=dict)

    def topology_edges(self, net: str) -> list[tuple[int, int]]:
        return [(edge.u, edge.v) for edge in self.trees.get(net, [])]

    def to_dict(self) -> dict[str, Any]:
        return {
            "vertices": sum(len(edge.vertices) for edge in self.hyperedges.values()),
            "hyperedges": len(self.hyperedges),
            "tree_edges": sum(len(edges) for edges in self.trees.values()),
            "conflict_edges": sum(len(v) for v in self.conflict_graph.values()) // 2,
            "layer_assignment": dict(self.layer_assignment),
            **self.metrics,
        }


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        return True


def _distance(a: GraphVertex, b: GraphVertex) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _orientation(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    cx: float,
    cy: float,
) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def segments_properly_cross(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
    *,
    tolerance: float = 1e-9,
) -> bool:
    """True for an interior 2-D crossing, excluding shared endpoints/touches."""
    if any(
        math.hypot(p[0] - q[0], p[1] - q[1]) <= tolerance
        for p in (a, b)
        for q in (c, d)
    ):
        return False
    o1 = _orientation(*a, *b, *c)
    o2 = _orientation(*a, *b, *d)
    o3 = _orientation(*c, *d, *a)
    o4 = _orientation(*c, *d, *b)
    return (o1 > tolerance and o2 < -tolerance or o1 < -tolerance and o2 > tolerance) and (
        o3 > tolerance and o4 < -tolerance or o3 < -tolerance and o4 > tolerance
    )


def _root_tree(edges: list[GraphEdge], vertex_count: int) -> list[GraphEdge]:
    """Return deterministic root-to-frontier order for native Prim growth."""
    if not edges or vertex_count < 2:
        return edges
    adjacency: dict[int, list[GraphEdge]] = {i: [] for i in range(vertex_count)}
    for edge in edges:
        adjacency[edge.u].append(edge)
        adjacency[edge.v].append(edge)
    for values in adjacency.values():
        values.sort(key=lambda e: (e.weight, e.length_mm, min(e.u, e.v), max(e.u, e.v)))
    ordered: list[GraphEdge] = []
    seen = {0}
    queue = [0]
    while queue:
        current = queue.pop(0)
        for edge in adjacency[current]:
            other = edge.v if edge.u == current else edge.u
            if other in seen:
                continue
            seen.add(other)
            queue.append(other)
            ordered.append(
                GraphEdge(
                    net=edge.net,
                    u=current,
                    v=other,
                    length_mm=edge.length_mm,
                    crossing_cost=edge.crossing_cost,
                    weight=edge.weight,
                )
            )
    return ordered


def minimum_spanning_tree(
    hyperedge: NetHyperedge,
    *,
    foreign_edges: Iterable[tuple[tuple[float, float], tuple[float, float]]] = (),
    crossing_penalty_mm: float = 0.0,
) -> list[GraphEdge]:
    """Kruskal tree with a geometric crossing penalty against foreign guides."""
    vertices = hyperedge.vertices
    if len(vertices) < 2:
        return []
    foreign = list(foreign_edges)
    candidates: list[GraphEdge] = []
    for i, a in enumerate(vertices):
        for j in range(i + 1, len(vertices)):
            b = vertices[j]
            crossings = sum(
                segments_properly_cross((a.x, a.y), (b.x, b.y), start, end)
                for start, end in foreign
            )
            length = _distance(a, b)
            candidates.append(
                GraphEdge(
                    net=hyperedge.net,
                    u=i,
                    v=j,
                    length_mm=length,
                    crossing_cost=crossings,
                    weight=length + crossing_penalty_mm * crossings,
                )
            )
    candidates.sort(key=lambda edge: (edge.weight, edge.length_mm, edge.u, edge.v))
    sets = _DisjointSet(len(vertices))
    selected: list[GraphEdge] = []
    for edge in candidates:
        if not sets.union(edge.u, edge.v):
            continue
        selected.append(edge)
        if len(selected) == len(vertices) - 1:
            break
    return _root_tree(selected, len(vertices))


def annular_spanning_tree(
    hyperedge: NetHyperedge,
    *,
    center: tuple[float, float],
    foreign_edges: Iterable[tuple[tuple[float, float], tuple[float, float]]] = (),
    crossing_penalty_mm: float = 0.0,
) -> list[GraphEdge] | None:
    """Build a low-degree lane topology for pads arranged on concentric rings.

    A Euclidean MST is optimal only for unconstrained straight edges.  On a
    circular PCB its spokes cut across every annular corridor.  This candidate
    instead chains each radial band in angular order, opens the chain at its
    largest/most-conflicted gap, and adds the minimum bridge between bands.
    ``None`` means the placement does not exhibit a reliable annular structure.
    """
    vertices = hyperedge.vertices
    if len(vertices) < 8:
        return None
    radii = [math.hypot(vertex.x - center[0], vertex.y - center[1]) for vertex in vertices]
    median_radius = statistics.median(radii)
    if median_radius <= 1e-6:
        return None

    core = [index for index, radius in enumerate(radii) if radius < 0.55 * median_radius]
    ring_indices = [index for index in range(len(vertices)) if index not in set(core)]
    if len(ring_indices) < max(6, int(0.75 * len(vertices))):
        return None

    # Split concentric bands at gaps larger than the placement noise expected
    # within one footprint ring.  HALO-90 yields two 9-pad bands plus one core.
    ordered_radial = sorted(ring_indices, key=lambda index: radii[index])
    split_gap = max(0.45, 0.05 * median_radius)
    bands: list[list[int]] = []
    for index in ordered_radial:
        if not bands or radii[index] - radii[bands[-1][-1]] > split_gap:
            bands.append([index])
        else:
            bands[-1].append(index)
    if not bands or len(bands) > 3 or any(len(band) < 3 for band in bands):
        return None
    if len(core) > max(3, int(0.2 * len(vertices))):
        return None

    foreign = list(foreign_edges)

    def edge(left: int, right: int) -> GraphEdge:
        a, b = vertices[left], vertices[right]
        crossings = sum(
            segments_properly_cross((a.x, a.y), (b.x, b.y), start, end)
            for start, end in foreign
        )
        length = _distance(a, b)
        return GraphEdge(
            net=hyperedge.net,
            u=left,
            v=right,
            length_mm=length,
            crossing_cost=crossings,
            weight=length + crossing_penalty_mm * crossings,
        )

    selected: list[GraphEdge] = []
    for band in bands:
        angular = sorted(
            band,
            key=lambda index: math.atan2(
                vertices[index].y - center[1], vertices[index].x - center[0]
            ),
        )
        closed = [edge(angular[index], angular[(index + 1) % len(angular)]) for index in range(len(angular))]
        # Opening the costliest wrap keeps a tree and leaves the most congested
        # angular sector available as a cross-ring escape channel.
        skip = max(range(len(closed)), key=lambda index: (closed[index].weight, index))
        selected.extend(value for index, value in enumerate(closed) if index != skip)

    # Join adjacent radial bands at their least-cost point.
    for left, right in zip(bands, bands[1:]):
        selected.append(min((edge(a, b) for a in left for b in right), key=lambda value: value.weight))

    if core:
        core_edge = NetHyperedge(
            net=hyperedge.net,
            vertices=[vertices[index] for index in core],
            priority=hyperedge.priority,
        )
        core_tree = minimum_spanning_tree(
            core_edge,
            foreign_edges=foreign,
            crossing_penalty_mm=crossing_penalty_mm,
        )
        # Map compact core indices back to the original hyperedge.
        selected.extend(
            edge(core[value.u], core[value.v]) for value in core_tree
        )
        selected.append(
            min(
                (edge(a, b) for a in core for b in bands[0]),
                key=lambda value: value.weight,
            )
        )

    if len(selected) != len(vertices) - 1:
        return None
    return _root_tree(selected, len(vertices))


def _tree_segments(
    hyperedge: NetHyperedge, edges: Iterable[GraphEdge]
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return [
        (
            (hyperedge.vertices[edge.u].x, hyperedge.vertices[edge.u].y),
            (hyperedge.vertices[edge.v].x, hyperedge.vertices[edge.v].y),
        )
        for edge in edges
    ]


def crossing_conflict_graph(
    hyperedges: dict[str, NetHyperedge], trees: dict[str, list[GraphEdge]]
) -> dict[str, dict[str, int]]:
    """Weighted net conflict graph: weight is projected guide crossings."""
    names = sorted(hyperedges)
    graph: dict[str, dict[str, int]] = {name: {} for name in names}
    segments = {
        name: _tree_segments(hyperedges[name], trees.get(name, [])) for name in names
    }
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            crossings = sum(
                segments_properly_cross(a, b, c, d)
                for a, b in segments[left]
                for c, d in segments[right]
            )
            if crossings:
                graph[left][right] = crossings
                graph[right][left] = crossings
    return graph


def dsatur_layer_coloring(
    graph: dict[str, dict[str, int]],
    hyperedges: dict[str, NetHyperedge],
    layers: list[str],
) -> dict[str, str]:
    """Weighted deterministic DSATUR coloring with pad-access penalties."""
    if not layers:
        layers = ["F.Cu"]
    colors: dict[str, str] = {}
    use_count = {layer: 0 for layer in layers}
    remaining = set(graph)
    while remaining:
        def priority(net: str) -> tuple[int, int, int, str]:
            used = {colors[n] for n in graph[net] if n in colors}
            weighted_degree = sum(graph[net].values())
            pins = len(hyperedges[net].vertices)
            return (len(used), weighted_degree, pins, net)

        net = max(remaining, key=priority)
        vertices = hyperedges[net].vertices
        best_layer = layers[0]
        best_cost: tuple[float, int, int] | None = None
        for layer_index, layer in enumerate(layers):
            conflict_cost = sum(
                weight for neighbor, weight in graph[net].items() if colors.get(neighbor) == layer
            )
            inaccessible = sum(1 for vertex in vertices if layer not in vertex.layers)
            # A non-exposed SMD layer is legal only through vias, so prefer an
            # exposed layer unless crossing reduction justifies the transition.
            cost = (float(conflict_cost) + inaccessible * 0.35, use_count[layer], layer_index)
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best_layer = layer
        colors[net] = best_layer
        use_count[best_layer] += 1
        remaining.remove(net)
    return colors


def build_board_hypergraph(
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    net_names: Iterable[str] | None = None,
) -> dict[str, NetHyperedge]:
    """Build pad vertices and multi-pin net hyperedges from a board model."""
    from physics_router.router import fanout_anchor

    copper = tuple(board.copper_layers or ["F.Cu", "B.Cu"])
    selected = list(net_names) if net_names is not None else list(board.nets)
    result: dict[str, NetHyperedge] = {}
    for net in selected:
        vertices: list[GraphVertex] = []
        seen: dict[tuple[float, float], int] = {}
        for ref, pad_number in board.nets.get(net, []):
            component = board.components.get(ref)
            if component is None:
                continue
            x, y = fanout_anchor(board, ref, net, pad_num=str(pad_number))
            pad = next(
                (
                    value
                    for value in component.pads or []
                    if str(value.get("num")) == str(pad_number)
                ),
                {},
            )
            raw_layers = tuple(str(layer) for layer in pad.get("layers") or [])
            allowed = (
                copper
                if "*.Cu" in raw_layers
                else tuple(layer for layer in copper if layer in raw_layers)
            )
            if not allowed:
                allowed = (copper[0],)
            key = (round(x, 3), round(y, 3))
            if key in seen:
                old = vertices[seen[key]]
                merged_layers = tuple(
                    layer
                    for layer in copper
                    if layer in set(old.layers) | set(allowed)
                )
                vertices[seen[key]] = GraphVertex(
                    index=old.index,
                    net=net,
                    ref=old.ref,
                    pad=old.pad,
                    x=old.x,
                    y=old.y,
                    layers=merged_layers,
                )
                continue
            seen[key] = len(vertices)
            vertices.append(
                GraphVertex(
                    index=len(vertices),
                    net=net,
                    ref=ref,
                    pad=str(pad_number),
                    x=x,
                    y=y,
                    layers=allowed,
                )
            )
        priority = config.weight_for_net(net) if config is not None else 1.0
        result[net] = NetHyperedge(net=net, vertices=vertices, priority=priority)
    return result


def plan_graph_topology(
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    net_names: Iterable[str] | None = None,
    layers: list[str] | None = None,
    crossing_penalty_mm: float | None = None,
) -> GraphTopologyPlan:
    """Plan crossing-aware net trees and color their conflict graph by layer."""
    copper = list(layers or board.copper_layers or ["F.Cu", "B.Cu"])
    hyperedges = build_board_hypergraph(board, config, net_names=net_names)
    # The placement—not the currently selected routing bucket—defines the
    # annular centre.  Exclusive one-net retries otherwise shift their median
    # toward that net's pads and fail to recognize the same ring topology.
    layout_points = [
        (component.x_mm, component.y_mm) for component in board.components.values()
    ]
    if not layout_points:
        layout_points = [
            (vertex.x, vertex.y)
            for edge in hyperedges.values()
            for vertex in edge.vertices
        ]
    layout_center = (
        statistics.median(point[0] for point in layout_points),
        statistics.median(point[1] for point in layout_points),
    ) if layout_points else (0.0, 0.0)

    def select_tree(
        edge: NetHyperedge,
        *,
        foreign_edges: Iterable[tuple[tuple[float, float], tuple[float, float]]] = (),
        penalty: float = 0.0,
    ) -> tuple[list[GraphEdge], bool]:
        foreign = list(foreign_edges)
        mst = minimum_spanning_tree(
            edge,
            foreign_edges=foreign,
            crossing_penalty_mm=penalty,
        )
        annular = annular_spanning_tree(
            edge,
            center=layout_center,
            foreign_edges=foreign,
            crossing_penalty_mm=penalty,
        )
        # Retain the lane topology when it is competitive with unconstrained
        # MST weight.  The small allowance pays for lower degree and preserved
        # annular corridors that the geometrizer can actually realize.
        if annular and sum(value.weight for value in annular) <= 1.12 * max(
            1e-9, sum(value.weight for value in mst)
        ):
            return annular, True
        return mst, False

    initial_selected = {name: select_tree(edge) for name, edge in hyperedges.items()}
    initial = {name: value[0] for name, value in initial_selected.items()}
    conflict = crossing_conflict_graph(hyperedges, initial)
    assignment = dsatur_layer_coloring(conflict, hyperedges, copper)
    penalty = crossing_penalty_mm
    if penalty is None:
        penalty = max(1.0, math.hypot(board.width_mm, board.height_mm) * 0.08)

    refined: dict[str, list[GraphEdge]] = {}
    annular_nets: list[str] = []
    for name in sorted(hyperedges, key=lambda n: (-hyperedges[n].priority, n)):
        same_layer_foreign: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for other, tree in initial.items():
            if other == name or assignment.get(other) != assignment.get(name):
                continue
            same_layer_foreign.extend(_tree_segments(hyperedges[other], tree))
        refined[name], used_annular = select_tree(
            hyperedges[name],
            foreign_edges=same_layer_foreign,
            penalty=penalty,
        )
        if used_annular:
            annular_nets.append(name)

    refined_conflict = crossing_conflict_graph(hyperedges, refined)
    projected_crossings = sum(
        weight
        for left, neighbors in refined_conflict.items()
        for right, weight in neighbors.items()
        if left < right
    )
    projected_same_layer_crossings = sum(
        weight
        for left, neighbors in refined_conflict.items()
        for right, weight in neighbors.items()
        if left < right and assignment.get(left) == assignment.get(right)
    )
    guide_length = sum(edge.length_mm for tree in refined.values() for edge in tree)
    max_degree = 0
    for name, tree in refined.items():
        degree = [0] * len(hyperedges[name].vertices)
        for edge in tree:
            degree[edge.u] += 1
            degree[edge.v] += 1
        max_degree = max(max_degree, max(degree, default=0))
    return GraphTopologyPlan(
        hyperedges=hyperedges,
        trees=refined,
        conflict_graph=refined_conflict,
        layer_assignment=assignment,
        metrics={
            "projected_same_layer_crossings": projected_same_layer_crossings,
            "projected_crossings": projected_crossings,
            "guide_length_mm": round(guide_length, 3),
            "layers_used": len(set(assignment.values())),
            "max_tree_degree": max_degree,
            "max_conflict_degree": max(
                (len(neighbors) for neighbors in refined_conflict.values()),
                default=0,
            ),
            "annular_nets": sorted(annular_nets),
            "annular_net_count": len(annular_nets),
            "planner": "hypergraph+crossing_mst+dsatur",
        },
    )


def _articulation_points_and_bridges(
    adjacency: dict[int, set[int]],
) -> tuple[set[int], set[tuple[int, int]]]:
    """Tarjan cut vertices/edges for one undirected embedded copper graph."""
    discovery: dict[int, int] = {}
    low: dict[int, int] = {}
    parent: dict[int, int] = {}
    articulation: set[int] = set()
    bridges: set[tuple[int, int]] = set()
    clock = 0

    def visit(vertex: int) -> None:
        nonlocal clock
        discovery[vertex] = clock
        low[vertex] = clock
        clock += 1
        children = 0
        for neighbor in sorted(adjacency.get(vertex, set())):
            if neighbor not in discovery:
                parent[neighbor] = vertex
                children += 1
                visit(neighbor)
                low[vertex] = min(low[vertex], low[neighbor])
                if vertex not in parent and children > 1:
                    articulation.add(vertex)
                if vertex in parent and low[neighbor] >= discovery[vertex]:
                    articulation.add(vertex)
                if low[neighbor] > discovery[vertex]:
                    bridges.add(tuple(sorted((vertex, neighbor))))
            elif parent.get(vertex) != neighbor:
                low[vertex] = min(low[vertex], discovery[neighbor])

    for vertex in sorted(adjacency):
        if vertex not in discovery:
            visit(vertex)
    return articulation, bridges


def analyze_route_graph(result: Any) -> dict[str, Any]:
    """Analyze emitted copper as an embedded multilayer graph."""
    segments = list(result.segments or [])
    vias = list(result.vias or [])
    by_net: dict[str, list[Any]] = {}
    for segment in segments:
        by_net.setdefault(segment.net, []).append(segment)

    crossing_pairs: set[tuple[str, str]] = set()
    crossing_number = 0
    for index, left in enumerate(segments):
        for right in segments[index + 1 :]:
            if left.net == right.net or left.layer != right.layer:
                continue
            if segments_properly_cross(
                (left.x1, left.y1),
                (left.x2, left.y2),
                (right.x1, right.y1),
                (right.x2, right.y2),
            ):
                crossing_number += 1
                crossing_pairs.add(tuple(sorted((left.net, right.net))))

    total_cycles = 0
    total_components = 0
    total_vertices = 0
    max_degree = 0
    total_articulation_points = 0
    total_bridges = 0
    per_net: dict[str, dict[str, Any]] = {}
    for net, net_segments in by_net.items():
        node_ids: dict[tuple[float, float, str], int] = {}

        def node(x: float, y: float, layer: str) -> int:
            key = (round(x, 3), round(y, 3), layer)
            if key not in node_ids:
                node_ids[key] = len(node_ids)
            return node_ids[key]

        edge_pairs: list[tuple[int, int]] = []
        degree: dict[int, int] = {}
        via_edges = 0
        for segment in net_segments:
            a = node(segment.x1, segment.y1, segment.layer)
            b = node(segment.x2, segment.y2, segment.layer)
            edge_pairs.append((a, b))
            degree[a] = degree.get(a, 0) + 1
            degree[b] = degree.get(b, 0) + 1
        for via in (value for value in vias if value.net == net):
            # A serialized through via records its span endpoints (normally
            # F.Cu/B.Cu), not every traversed inner layer. Include route layers
            # terminating at its coordinate so diagnostics match KiCad's
            # vertical connectivity.
            vx = round(via.x, 3)
            vy = round(via.y, 3)
            touched_layers = {
                segment.layer
                for segment in net_segments
                if (round(segment.x1, 3), round(segment.y1, 3)) == (vx, vy)
                or (round(segment.x2, 3), round(segment.y2, 3)) == (vx, vy)
            }
            layers = tuple(
                dict.fromkeys((*tuple(via.layers or ()), *sorted(touched_layers)))
            )
            if len(layers) < 2:
                continue
            root = node(via.x, via.y, layers[0])
            for layer in layers[1:]:
                target = node(via.x, via.y, layer)
                edge_pairs.append((root, target))
                via_edges += 1
                degree[root] = degree.get(root, 0) + 1
                degree[target] = degree.get(target, 0) + 1
        sets = _DisjointSet(len(node_ids))
        for a, b in edge_pairs:
            sets.union(a, b)
        components = len({sets.find(index) for index in range(len(node_ids))}) if node_ids else 0
        cycles = max(0, len(edge_pairs) - len(node_ids) + components)
        adjacency = {index: set() for index in range(len(node_ids))}
        for a, b in edge_pairs:
            adjacency[a].add(b)
            adjacency[b].add(a)
        articulation, bridges = _articulation_points_and_bridges(adjacency)
        total_vertices += len(node_ids)
        total_components += components
        total_cycles += cycles
        total_articulation_points += len(articulation)
        total_bridges += len(bridges)
        max_degree = max(max_degree, max(degree.values(), default=0))
        per_net[net] = {
            "vertices": len(node_ids),
            "edges": len(edge_pairs),
            "via_edges": via_edges,
            "connected_components": components,
            "cycle_rank": cycles,
            "articulation_points": len(articulation),
            "bridges": len(bridges),
            "max_degree": max(degree.values(), default=0),
        }

    layer_usage: dict[str, int] = {}
    for segment in segments:
        layer_usage[segment.layer] = layer_usage.get(segment.layer, 0) + 1
    return {
        "vertices": total_vertices,
        "edges": len(segments) + len(vias),
        "connected_components": total_components,
        "cycle_rank": total_cycles,
        "crossing_number": crossing_number,
        "crossing_conflict_edges": len(crossing_pairs),
        "max_degree": max_degree,
        "articulation_points": total_articulation_points,
        "bridges": total_bridges,
        "layer_edge_counts": layer_usage,
        "per_net": per_net,
    }
