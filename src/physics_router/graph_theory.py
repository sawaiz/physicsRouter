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
    occupancy: dict[tuple[int, int], float] | None = None,
    cell_mm: float = 1.0,
    overflow_penalty_mm: float = 0.0,
) -> list[GraphEdge]:
    """Kruskal tree with geometric crossing + optional overflow costs."""
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
            overflow = _segment_overflow(
                (a.x, a.y), (b.x, b.y), occupancy, cell_mm=cell_mm
            )
            candidates.append(
                GraphEdge(
                    net=hyperedge.net,
                    u=i,
                    v=j,
                    length_mm=length,
                    crossing_cost=crossings,
                    weight=length
                    + crossing_penalty_mm * crossings
                    + overflow_penalty_mm * overflow,
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


def _segment_overflow(
    a: tuple[float, float],
    b: tuple[float, float],
    occupancy: dict[tuple[int, int], float] | None,
    *,
    cell_mm: float = 1.0,
) -> float:
    """Sample max occupancy along a straight guide (overflow-aware Steiner/MST)."""
    if not occupancy:
        return 0.0
    cell = max(0.2, float(cell_mm))
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    samples = max(2, int(math.ceil(length / (0.5 * cell))))
    peak = 0.0
    for i in range(samples + 1):
        t = i / samples
        x = a[0] + (b[0] - a[0]) * t
        y = a[1] + (b[1] - a[1]) * t
        key = (int(math.floor(x / cell)), int(math.floor(y / cell)))
        peak = max(peak, float(occupancy.get(key, 0.0)))
    return peak


def _steiner_candidate_points(
    vertices: list[GraphVertex],
    *,
    max_extra: int = 48,
) -> list[tuple[float, float]]:
    """Hanan-style + edge-midpoint Steiner candidates for multipin nets.

    Graph theory: Steiner vertices in the metric closure improve multipin trees
    under congestion (NeuralSteiner / classical RST construction). We keep a
    bounded set for deterministic Kruskal on terminals ∪ candidates.
    """
    if len(vertices) < 3:
        return []
    xs = sorted({round(v.x, 4) for v in vertices})
    ys = sorted({round(v.y, 4) for v in vertices})
    # Hanan grid intersections (not coinciding with terminals)
    term = {(round(v.x, 4), round(v.y, 4)) for v in vertices}
    hanan: list[tuple[float, float]] = []
    for x in xs:
        for y in ys:
            p = (x, y)
            if p not in term:
                hanan.append(p)
    # Edge midpoints (local Steiner for long branches)
    mids: list[tuple[float, float]] = []
    for i, a in enumerate(vertices):
        for j in range(i + 1, len(vertices)):
            b = vertices[j]
            mids.append((0.5 * (a.x + b.x), 0.5 * (a.y + b.y)))
    # Prefer midpoints near the geometric median, then Hanan
    cx = statistics.median(v.x for v in vertices)
    cy = statistics.median(v.y for v in vertices)

    def dist_key(p: tuple[float, float]) -> float:
        return math.hypot(p[0] - cx, p[1] - cy)

    # Dedup
    seen: set[tuple[float, float]] = set()
    ordered: list[tuple[float, float]] = []
    for p in sorted(mids, key=dist_key) + sorted(hanan, key=dist_key):
        key = (round(p[0], 3), round(p[1], 3))
        if key in seen or key in term:
            continue
        # Skip midpoints that coincide with a pin
        if any(math.hypot(p[0] - v.x, p[1] - v.y) < 1e-3 for v in vertices):
            continue
        seen.add(key)
        ordered.append(p)
        if len(ordered) >= max_extra:
            break
    return ordered


def overflow_aware_steiner_tree(
    hyperedge: NetHyperedge,
    *,
    foreign_edges: Iterable[tuple[tuple[float, float], tuple[float, float]]] = (),
    crossing_penalty_mm: float = 0.0,
    occupancy: dict[tuple[int, int], float] | None = None,
    cell_mm: float = 1.0,
    overflow_penalty_mm: float = 2.0,
    max_steiner: int = 48,
) -> list[GraphEdge]:
    """Approximate Steiner tree on pins + overflow-weighted candidate points.

    Algorithm (classical Steiner heuristic on an extended metric):
    1. Add bounded Hanan/midpoint Steiner candidates.
    2. Kruskal MST on terminals ∪ candidates with weight =
       length + crossing_penalty·crossings + overflow_penalty·max_occupancy.
    3. Iteratively prune degree-1 non-terminals (Steiner leaves).
    4. Map remaining edges back to terminal indices only when both ends are
       terminals; otherwise split paths through Steiner points into a tree on
       the original pin indices by contracting Steiner paths.

    Falls back to :func:`minimum_spanning_tree` when steiner gains nothing.
    """
    terminals = hyperedge.vertices
    n_term = len(terminals)
    if n_term < 2:
        return []
    if n_term == 2:
        return minimum_spanning_tree(
            hyperedge,
            foreign_edges=foreign_edges,
            crossing_penalty_mm=crossing_penalty_mm,
            occupancy=occupancy,
            cell_mm=cell_mm,
            overflow_penalty_mm=overflow_penalty_mm,
        )

    foreign = list(foreign_edges)
    steiner_pts = _steiner_candidate_points(terminals, max_extra=max_steiner)
    # Points: [0..n_term) terminals, then Steiner
    points: list[tuple[float, float]] = [(v.x, v.y) for v in terminals]
    points.extend(steiner_pts)
    n_all = len(points)
    is_terminal = [i < n_term for i in range(n_all)]

    candidates: list[tuple[float, float, int, int, int, float]] = []
    # weight, length, u, v, crossings, overflow
    for i in range(n_all):
        for j in range(i + 1, n_all):
            a, b = points[i], points[j]
            length = math.hypot(a[0] - b[0], a[1] - b[1])
            # Skip very long Steiner-only edges (keeps graph sparse-ish)
            if not is_terminal[i] and not is_terminal[j] and length > 0:
                pair_d = [
                    _distance(terminals[p], terminals[q])
                    for p in range(n_term)
                    for q in range(p + 1, n_term)
                ]
                med = statistics.median(pair_d) if pair_d else 1.0
                if length > 3.0 * max(med, 1e-3):
                    continue
            crossings = sum(
                segments_properly_cross(a, b, start, end) for start, end in foreign
            )
            overflow = _segment_overflow(a, b, occupancy, cell_mm=cell_mm)
            weight = (
                length
                + crossing_penalty_mm * crossings
                + overflow_penalty_mm * overflow
            )
            candidates.append((weight, length, i, j, crossings, overflow))
    candidates.sort(key=lambda t: (t[0], t[1], t[2], t[3]))

    dsu = _DisjointSet(n_all)
    mst_edges: list[tuple[int, int, float, int, float]] = []  # u,v,len,cross,w
    for weight, length, u, v, crossings, _ov in candidates:
        if not dsu.union(u, v):
            continue
        mst_edges.append((u, v, length, crossings, weight))
        if len(mst_edges) >= n_all - 1:
            break

    # Adjacency for pruning
    adj: dict[int, list[tuple[int, float, int, float]]] = {i: [] for i in range(n_all)}
    for u, v, length, crossings, weight in mst_edges:
        adj[u].append((v, length, crossings, weight))
        adj[v].append((u, length, crossings, weight))

    # Prune degree-1 Steiner vertices
    changed = True
    while changed:
        changed = False
        for i in range(n_term, n_all):
            if len(adj[i]) == 1:
                nbr, length, crossings, weight = adj[i][0]
                adj[i].clear()
                adj[nbr] = [e for e in adj[nbr] if e[0] != i]
                changed = True
            elif len(adj[i]) == 0 and i >= n_term:
                pass

    # Contract paths through remaining Steiner points into terminal-terminal edges
    # by walking the forest restricted to edges still in adj.
    term_edges: list[GraphEdge] = []
    visited_undirected: set[tuple[int, int]] = set()

    def walk_from_terminal(start: int) -> None:
        stack = [(start, start, 0.0, 0, -1)]  # node, path_start_term, length, cross, parent
        while stack:
            node, origin, acc_len, acc_cross, parent = stack.pop()
            for nbr, length, crossings, _w in adj[node]:
                if nbr == parent:
                    continue
                key = (min(node, nbr), max(node, nbr))
                if key in visited_undirected and node != start:
                    continue
                new_len = acc_len + length
                new_cross = acc_cross + crossings
                if is_terminal[nbr] and nbr != origin:
                    ekey = (min(origin, nbr), max(origin, nbr))
                    if ekey not in visited_undirected:
                        visited_undirected.add(ekey)
                        term_edges.append(
                            GraphEdge(
                                net=hyperedge.net,
                                u=origin,
                                v=nbr,
                                length_mm=new_len,
                                crossing_cost=new_cross,
                                weight=new_len + crossing_penalty_mm * new_cross,
                            )
                        )
                    # continue through terminal? no — start new walks from each terminal
                elif not is_terminal[nbr]:
                    visited_undirected.add(key)
                    stack.append((nbr, origin, new_len, new_cross, node))

    for t in range(n_term):
        walk_from_terminal(t)

    # Kruskal on terminal edges to ensure a tree (walk may over-generate)
    term_edges.sort(key=lambda e: (e.weight, e.length_mm, e.u, e.v))
    dsu_t = _DisjointSet(n_term)
    selected: list[GraphEdge] = []
    for edge in term_edges:
        if not dsu_t.union(edge.u, edge.v):
            continue
        selected.append(edge)
        if len(selected) == n_term - 1:
            break

    # Fallback if steiner contraction failed to connect all terminals
    if len(selected) < n_term - 1:
        return minimum_spanning_tree(
            hyperedge,
            foreign_edges=foreign,
            crossing_penalty_mm=crossing_penalty_mm,
            occupancy=occupancy,
            cell_mm=cell_mm,
            overflow_penalty_mm=overflow_penalty_mm,
        )

    # Prefer steiner only if it improves weight vs pure MST under same costs
    mst = minimum_spanning_tree(
        hyperedge,
        foreign_edges=foreign,
        crossing_penalty_mm=crossing_penalty_mm,
        occupancy=occupancy,
        cell_mm=cell_mm,
        overflow_penalty_mm=overflow_penalty_mm,
    )
    steiner_w = sum(e.weight for e in selected)
    mst_w = sum(e.weight for e in mst)
    if steiner_w > mst_w * 1.001:
        return mst
    return _root_tree(selected, n_term)


def occupancy_from_trees(
    hyperedges: dict[str, NetHyperedge],
    trees: dict[str, list[GraphEdge]],
    *,
    cell_mm: float = 1.0,
) -> dict[tuple[int, int], float]:
    """Rasterize guide trees into a coarse occupancy map (for overflow Steiner)."""
    cell = max(0.2, float(cell_mm))
    occ: dict[tuple[int, int], float] = {}
    for net, edges in trees.items():
        verts = hyperedges[net].vertices
        for edge in edges:
            a = verts[edge.u]
            b = verts[edge.v]
            length = math.hypot(a.x - b.x, a.y - b.y)
            samples = max(2, int(math.ceil(length / (0.5 * cell))))
            for i in range(samples + 1):
                t = i / samples
                x = a.x + (b.x - a.x) * t
                y = a.y + (b.y - a.y) * t
                key = (int(math.floor(x / cell)), int(math.floor(y / cell)))
                occ[key] = occ.get(key, 0.0) + 1.0
    return occ


@dataclass(frozen=True)
class CutCertificate:
    """Min-cut style capacity certificate for a coarse geometric cut."""

    kind: str
    description: str
    demand: int
    capacity: int
    nets_forced: tuple[str, ...]
    coordinate: float

    @property
    def saturated(self) -> bool:
        return self.demand > self.capacity

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "description": self.description,
            "demand": self.demand,
            "capacity": self.capacity,
            "nets_forced": list(self.nets_forced),
            "coordinate": round(self.coordinate, 3),
            "saturated": self.saturated,
            "slack": self.capacity - self.demand,
        }


def cut_capacity_preflight(
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    net_names: Iterable[str] | None = None,
    track_pitch_mm: float | None = None,
    copper_layers: int | None = None,
) -> dict[str, Any]:
    """Certify coarse cut capacities vs nets forced to cross (impossibility).

    Graph theory: multi-commodity demand across a cut cannot exceed cut capacity.
    For each axis-aligned cut through the board, count nets with pins on both
    sides (forced crossings) and estimate track slots as
    ``floor(cut_length / pitch) * signal_layers``.

    Saturated cuts prove the current stackup/pitch cannot complete all nets
    without layer changes that share the same scarce corridor — the router
    should prefer open nets over thrashing.
    """
    hyperedges = build_board_hypergraph(board, config, net_names=net_names)
    pitch = float(
        track_pitch_mm
        if track_pitch_mm is not None
        else max(0.25, 0.3)  # conservative default
    )
    # Prefer rules-like pitch if config carries nothing — use min of board size scale
    if track_pitch_mm is None:
        pitch = max(0.2, min(board.width_mm, board.height_mm) * 0.01)
        pitch = max(0.2, min(pitch, 0.5))
    n_layers = int(copper_layers or len(board.copper_layers or ["F.Cu", "B.Cu"]))
    # Signal-ish layers: all copper layers as upper bound on parallel tracks
    certificates: list[CutCertificate] = []

    def pins_for_net(he: NetHyperedge) -> list[tuple[float, float]]:
        return [(v.x, v.y) for v in he.vertices]

    # Vertical cuts
    for frac in (0.25, 0.5, 0.75):
        x_cut = board.width_mm * frac
        forced: list[str] = []
        for name, he in hyperedges.items():
            pts = pins_for_net(he)
            if len(pts) < 2:
                continue
            left = any(p[0] < x_cut - 1e-6 for p in pts)
            right = any(p[0] > x_cut + 1e-6 for p in pts)
            if left and right:
                forced.append(name)
        # Capacity: vertical cut length = board height
        cap = max(1, int(math.floor(board.height_mm / max(0.1, pitch)))) * max(1, n_layers)
        certificates.append(
            CutCertificate(
                kind="vertical",
                description=f"x={x_cut:.2f}mm (frac={frac})",
                demand=len(forced),
                capacity=cap,
                nets_forced=tuple(sorted(forced)),
                coordinate=x_cut,
            )
        )

    # Horizontal cuts
    for frac in (0.25, 0.5, 0.75):
        y_cut = board.height_mm * frac
        forced = []
        for name, he in hyperedges.items():
            pts = pins_for_net(he)
            if len(pts) < 2:
                continue
            below = any(p[1] < y_cut - 1e-6 for p in pts)
            above = any(p[1] > y_cut + 1e-6 for p in pts)
            if below and above:
                forced.append(name)
        cap = max(1, int(math.floor(board.width_mm / max(0.1, pitch)))) * max(1, n_layers)
        certificates.append(
            CutCertificate(
                kind="horizontal",
                description=f"y={y_cut:.2f}mm (frac={frac})",
                demand=len(forced),
                capacity=cap,
                nets_forced=tuple(sorted(forced)),
                coordinate=y_cut,
            )
        )

    saturated = [c for c in certificates if c.saturated]
    worst = max(certificates, key=lambda c: c.demand - c.capacity) if certificates else None
    return {
        "algorithm": "geometric_cut_capacity_preflight",
        "track_pitch_mm": round(pitch, 4),
        "copper_layers": n_layers,
        "certificates": [c.to_dict() for c in certificates],
        "saturated_cuts": len(saturated),
        "saturated": [c.to_dict() for c in saturated],
        "worst": worst.to_dict() if worst else None,
        "feasible_under_model": len(saturated) == 0,
        "notes": [
            "Demand = nets with pins on both sides of the cut (must cross).",
            "Capacity = floor(cut_length/pitch)*copper_layers (upper bound).",
            "Saturated ⇒ no packing of all forced nets without sharing scarce corridors.",
        ],
    }


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
    use_overflow_steiner: bool = True,
    overflow_penalty_mm: float = 2.0,
    cell_mm: float | None = None,
    run_cut_preflight: bool = True,
    track_pitch_mm: float | None = None,
) -> GraphTopologyPlan:
    """Plan crossing/overflow-aware net trees and color conflict graph by layer.

    When ``use_overflow_steiner`` is True, multipin nets (≥3 pins) are planned
    with :func:`overflow_aware_steiner_tree` under a raster occupancy map from
    already-placed higher-priority guides (NeuralSteiner-style overflow avoidance
    without a neural net). Cut-capacity certificates are attached in metrics.
    """
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
    cell = float(cell_mm or max(0.5, min(board.width_mm, board.height_mm) * 0.04))

    def select_tree(
        edge: NetHyperedge,
        *,
        foreign_edges: Iterable[tuple[tuple[float, float], tuple[float, float]]] = (),
        penalty: float = 0.0,
        occupancy: dict[tuple[int, int], float] | None = None,
    ) -> tuple[list[GraphEdge], str]:
        foreign = list(foreign_edges)
        mst = minimum_spanning_tree(
            edge,
            foreign_edges=foreign,
            crossing_penalty_mm=penalty,
            occupancy=occupancy,
            cell_mm=cell,
            overflow_penalty_mm=overflow_penalty_mm if occupancy else 0.0,
        )
        annular = annular_spanning_tree(
            edge,
            center=layout_center,
            foreign_edges=foreign,
            crossing_penalty_mm=penalty,
        )
        steiner: list[GraphEdge] | None = None
        if use_overflow_steiner and len(edge.vertices) >= 3:
            steiner = overflow_aware_steiner_tree(
                edge,
                foreign_edges=foreign,
                crossing_penalty_mm=penalty,
                occupancy=occupancy,
                cell_mm=cell,
                overflow_penalty_mm=overflow_penalty_mm if occupancy else 0.0,
            )
        # Rank candidates: annular if competitive, else best of steiner/mst
        mst_w = sum(value.weight for value in mst)
        candidates: list[tuple[float, str, list[GraphEdge]]] = [(mst_w, "mst", mst)]
        if annular:
            ann_w = sum(value.weight for value in annular)
            if ann_w <= 1.12 * max(1e-9, mst_w):
                candidates.append((ann_w, "annular", annular))
        if steiner is not None:
            st_w = sum(value.weight for value in steiner)
            candidates.append((st_w, "steiner", steiner))
        candidates.sort(key=lambda t: (t[0], t[1]))
        best_w, kind, best_tree = candidates[0]
        return best_tree, kind

    penalty = crossing_penalty_mm
    if penalty is None:
        penalty = max(1.0, math.hypot(board.width_mm, board.height_mm) * 0.08)

    # Priority order for progressive occupancy (high priority paints first)
    order = sorted(hyperedges, key=lambda n: (-hyperedges[n].priority, n))
    initial: dict[str, list[GraphEdge]] = {}
    initial_kind: dict[str, str] = {}
    occ: dict[tuple[int, int], float] = {}
    for name in order:
        tree, kind = select_tree(
            hyperedges[name],
            penalty=penalty,
            occupancy=occ if use_overflow_steiner else None,
        )
        initial[name] = tree
        initial_kind[name] = kind
        # Paint this net into occupancy for later nets
        tmp = occupancy_from_trees(
            {name: hyperedges[name]}, {name: tree}, cell_mm=cell
        )
        for k, v in tmp.items():
            occ[k] = occ.get(k, 0.0) + v

    conflict = crossing_conflict_graph(hyperedges, initial)
    assignment = dsatur_layer_coloring(conflict, hyperedges, copper)

    refined: dict[str, list[GraphEdge]] = {}
    annular_nets: list[str] = []
    steiner_nets: list[str] = []
    refined_kind: dict[str, str] = {}
    occ_refined: dict[tuple[int, int], float] = {}
    for name in order:
        same_layer_foreign: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for other, tree in initial.items():
            if other == name or assignment.get(other) != assignment.get(name):
                continue
            same_layer_foreign.extend(_tree_segments(hyperedges[other], tree))
        refined[name], kind = select_tree(
            hyperedges[name],
            foreign_edges=same_layer_foreign,
            penalty=penalty,
            occupancy=occ_refined if use_overflow_steiner else None,
        )
        refined_kind[name] = kind
        if kind == "annular":
            annular_nets.append(name)
        if kind == "steiner":
            steiner_nets.append(name)
        tmp = occupancy_from_trees(
            {name: hyperedges[name]}, {name: refined[name]}, cell_mm=cell
        )
        for k, v in tmp.items():
            occ_refined[k] = occ_refined.get(k, 0.0) + v

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

    cut_report = None
    if run_cut_preflight:
        cut_report = cut_capacity_preflight(
            board,
            config,
            net_names=net_names,
            track_pitch_mm=track_pitch_mm,
            copper_layers=len(copper),
        )

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
            "steiner_nets": sorted(steiner_nets),
            "steiner_net_count": len(steiner_nets),
            "tree_kinds": dict(sorted(refined_kind.items())),
            "overflow_steiner": use_overflow_steiner,
            "occupancy_cells": len(occ_refined),
            "cut_preflight": cut_report,
            "planner": "hypergraph+overflow_steiner+annular+dsatur+cut_preflight",
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
