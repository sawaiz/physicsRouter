"""Hierarchical capacity mesh (inspired by tscircuit capacity-autorouter, MIT).

Global planning subdivides the board into capacity cells whose size tracks how
many tracks/vias they can absorb. Detailed routing still owns exact geometry;
this mesh only negotiates *where* corridors and layers are oversubscribed.

References (ideas, not code copy):
  https://github.com/tscircuit/tscircuit-autorouter
  blog.autorouting.com — hypergraph / capacity-mesh autorouting
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from physics_router.design_rules import DesignRules
from physics_router.models import BoardModel


@dataclass(frozen=True)
class CapacityNode:
    """Axis-aligned capacity cell (optionally multi-layer via available_z)."""

    node_id: str
    cx: float
    cy: float
    width: float
    height: float
    depth: int
    available_layers: tuple[str, ...]
    capacity: float
    contains_target: bool = False
    contains_obstacle: bool = False
    completely_inside_obstacle: bool = False

    @property
    def min_x(self) -> float:
        return self.cx - 0.5 * self.width

    @property
    def max_x(self) -> float:
        return self.cx + 0.5 * self.width

    @property
    def min_y(self) -> float:
        return self.cy - 0.5 * self.height

    @property
    def max_y(self) -> float:
        return self.cy + 0.5 * self.height

    def contains_point(self, x: float, y: float) -> bool:
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y


@dataclass(frozen=True)
class CapacityEdge:
    edge_id: str
    a: str
    b: str
    same_layer: bool


@dataclass
class CapacityMesh:
    nodes: list[CapacityNode]
    edges: list[CapacityEdge]
    layers: list[str]
    metrics: dict[str, Any] = field(default_factory=dict)

    def node_map(self) -> dict[str, CapacityNode]:
        return {n.node_id: n for n in self.nodes}

    def adjacency(self) -> dict[str, list[str]]:
        adj: dict[str, list[str]] = defaultdict(list)
        for e in self.edges:
            adj[e.a].append(e.b)
            adj[e.b].append(e.a)
        return dict(adj)

    def leaf_nodes(self) -> list[CapacityNode]:
        # All nodes kept are leaves after subdivision (parents discarded)
        return list(self.nodes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": "capacity_mesh_quadtree",
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "layers": list(self.layers),
            "metrics": dict(self.metrics),
            "capacity_hist": _capacity_histogram(self.nodes),
        }


def tuned_node_capacity(
    width: float,
    height: float,
    *,
    via_diameter_mm: float = 0.6,
    track_pitch_mm: float = 0.35,
    layer_count: int = 2,
    max_capacity_factor: float = 1.0,
) -> float:
    """Estimate how many routes a node can absorb (tscircuit-style tuning).

    Capacity is continuous so negotiated congestion can use fractional overflow.
    Single-layer-only nodes are capped at 1.0 when the estimate would be higher.
    """
    if width <= 0 or height <= 0:
        return 0.0
    min_side = min(width, height)
    span = math.sqrt(width * height)
    pitch = max(0.08, via_diameter_mm * 0.5 + track_pitch_mm)
    via_ratio = min_side / max(0.08, via_diameter_mm + 0.5 * track_pitch_mm)
    via_ratio_factor = min(1.2, max(0.85, via_ratio**0.05))
    via_length_across = (span * via_ratio_factor) / pitch
    cap = (via_length_across / 2.0) ** 1.1 * max_capacity_factor
    if layer_count <= 1 and cap > 1.0:
        return 1.0
    return max(0.0, float(cap))


def calculate_optimal_capacity_depth(
    board_span_mm: float,
    *,
    target_min_capacity: float = 0.55,
    max_depth: int = 12,
    via_diameter_mm: float = 0.6,
    track_pitch_mm: float = 0.35,
) -> int:
    """Subdivide until leaf capacity approaches *target_min_capacity*."""
    depth = 0
    width = max(1.0, board_span_mm)
    while depth < max_depth:
        cap = tuned_node_capacity(
            width,
            width,
            via_diameter_mm=via_diameter_mm,
            track_pitch_mm=track_pitch_mm,
        )
        if cap <= target_min_capacity:
            break
        width *= 0.5
        depth += 1
    return max(1, depth)


def build_capacity_mesh(
    board: BoardModel,
    rules: DesignRules,
    *,
    capacity_depth: int | None = None,
    effort: float = 0.5,
    targets: Iterable[tuple[float, float, str]] | None = None,
) -> CapacityMesh:
    """Build a hierarchical capacity mesh covering the board extent.

    Prefers the C++ ``pr_native`` implementation when available; falls back to
    pure Python. ``targets`` are (x, y, net) pin anchors.
    """
    layers = list(board.copper_layers or ["F.Cu", "B.Cu"])
    # --- Native C++ path ---
    try:
        from physics_router.router import _native_core

        n = _native_core()
        if hasattr(n, "build_capacity_mesh"):
            cfg = n.RouteConfig()
            cfg.x_min, cfg.x_max = 0.0, float(board.width_mm)
            cfg.y_min, cfg.y_max = 0.0, float(board.height_mm)
            if board.outline:
                xs, ys = [], []
                for item in board.outline:
                    if item.get("kind") == "line":
                        xs += [float(item["x1"]), float(item["x2"])]
                        ys += [float(item["y1"]), float(item["y2"])]
                if xs:
                    cfg.x_min, cfg.x_max = min(xs), max(xs)
                    cfg.y_min, cfg.y_max = min(ys), max(ys)
            cfg.num_layers = max(1, len(layers))
            cfg.clearance_mm = float(rules.constraints.min_clearance_mm)
            cfg.via_diameter_mm = float(
                getattr(rules.constraints, "min_via_diameter_mm", None) or 0.6
            )
            t_list: list[tuple[float, float]] = []
            if targets:
                t_list = [(float(x), float(y)) for x, y, _n in targets]
            else:
                for _net, pins in board.nets.items():
                    for ref, _pad in pins:
                        if ref in board.components:
                            c = board.components[ref]
                            t_list.append((c.x_mm, c.y_mm))
            o_list = [
                (c.x_mm, c.y_mm) for c in board.components.values()
            ]
            raw = n.build_capacity_mesh(
                cfg,
                t_list,
                o_list,
                float(effort),
                int(capacity_depth) if capacity_depth is not None else -1,
            )
            nodes = [
                CapacityNode(
                    node_id=f"cn{nd.id}",
                    cx=float(nd.cx),
                    cy=float(nd.cy),
                    width=float(nd.width),
                    height=float(nd.height),
                    depth=int(nd.depth),
                    available_layers=tuple(layers),
                    capacity=float(nd.capacity),
                    contains_target=bool(nd.contains_target),
                    contains_obstacle=bool(nd.contains_obstacle),
                )
                for nd in raw.nodes
            ]
            # Rebuild edges via path queries is heavy; re-link from native edges
            # if exposed — currently only node list is bound; run Python
            # adjacency for edges so path_through_mesh still works.
            mesh = CapacityMesh(
                nodes=nodes,
                edges=[],
                layers=layers,
                metrics={
                    "capacity_depth": int(raw.capacity_depth),
                    "effort": float(raw.effort),
                    "board_span_mm": float(raw.board_span_mm),
                    "backend": "native_cpp",
                    "nodes": len(nodes),
                    "num_edges_native": int(raw.num_edges),
                },
            )
            # Python adjacency on native nodes
            return _link_python_edges(mesh)
    except Exception:
        pass
    # --- Pure Python fallback continues below ---
    x0, y0 = 0.0, 0.0
    x1, y1 = float(board.width_mm), float(board.height_mm)
    # Prefer outline bbox when available
    if board.outline:
        xs: list[float] = []
        ys: list[float] = []
        for item in board.outline:
            if item.get("kind") == "line":
                xs.extend([float(item["x1"]), float(item["x2"])])
                ys.extend([float(item["y1"]), float(item["y2"])])
            elif item.get("kind") == "circle":
                r = float(item.get("r") or 0)
                xs.extend([float(item["cx"]) - r, float(item["cx"]) + r])
                ys.extend([float(item["cy"]) - r, float(item["cy"]) + r])
        if xs and ys:
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
    for c in board.components.values():
        x0 = min(x0, c.x_mm - 0.5 * c.width_mm)
        x1 = max(x1, c.x_mm + 0.5 * c.width_mm)
        y0 = min(y0, c.y_mm - 0.5 * c.height_mm)
        y1 = max(y1, c.y_mm + 0.5 * c.height_mm)

    span = max(x1 - x0, y1 - y0, 1.0)
    cx = 0.5 * (x0 + x1)
    cy = 0.5 * (y0 + y1)
    # Square root covering board (matches tscircuit initial node)
    side = span * 1.02

    pitch = (
        rules.constraints.min_track_width_mm + rules.constraints.min_clearance_mm
    )
    via_d = float(getattr(rules.constraints, "min_via_diameter_mm", None) or 0.6)
    effort = max(0.05, min(1.0, float(effort)))
    depth = capacity_depth
    if depth is None:
        depth = calculate_optimal_capacity_depth(
            side,
            target_min_capacity=0.45 + 0.4 * (1.0 - effort),
            max_depth=int(6 + 8 * effort),
            via_diameter_mm=via_d,
            track_pitch_mm=pitch,
        )

    # Targets from anchors
    target_pts: list[tuple[float, float, str]] = list(targets or [])
    if not target_pts:
        for net, pins in board.nets.items():
            for ref, _pad in pins:
                if ref in board.components:
                    c = board.components[ref]
                    target_pts.append((c.x_mm, c.y_mm, net))

    # Obstacle centers (components)
    obstacles = [
        (c.x_mm, c.y_mm, max(c.width_mm, c.height_mm) * 0.5)
        for c in board.components.values()
    ]

    def node_flags(cx_n: float, cy_n: float, w: float, h: float) -> tuple[bool, bool, bool]:
        half_w, half_h = 0.5 * w, 0.5 * h
        contains_t = False
        contains_o = False
        completely_inside = False
        for tx, ty, _net in target_pts:
            if abs(tx - cx_n) <= half_w and abs(ty - cy_n) <= half_h:
                contains_t = True
                break
        for ox, oy, orad in obstacles:
            # loose overlap test
            if abs(ox - cx_n) <= half_w + orad and abs(oy - cy_n) <= half_h + orad:
                contains_o = True
                if abs(ox - cx_n) + orad < half_w * 0.35 and abs(oy - cy_n) + orad < half_h * 0.35:
                    completely_inside = True
        return contains_t, contains_o, completely_inside

    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"cn{counter}"

    root_cap = tuned_node_capacity(
        side, side, via_diameter_mm=via_d, track_pitch_mm=pitch, layer_count=len(layers)
    )
    unfinished: list[CapacityNode] = [
        CapacityNode(
            node_id=next_id(),
            cx=cx,
            cy=cy,
            width=side,
            height=side,
            depth=0,
            available_layers=tuple(layers),
            capacity=root_cap,
            contains_target=True,
            contains_obstacle=True,
        )
    ]
    finished: list[CapacityNode] = []

    max_iters = 50_000
    iters = 0
    while unfinished and iters < max_iters:
        iters += 1
        node = unfinished.pop()
        ct, co, full_obs = node_flags(node.cx, node.cy, node.width, node.height)
        # Refresh flags
        node = CapacityNode(
            node_id=node.node_id,
            cx=node.cx,
            cy=node.cy,
            width=node.width,
            height=node.height,
            depth=node.depth,
            available_layers=node.available_layers,
            capacity=tuned_node_capacity(
                node.width,
                node.height,
                via_diameter_mm=via_d,
                track_pitch_mm=pitch,
                layer_count=len(node.available_layers),
            ),
            contains_target=ct,
            contains_obstacle=co,
            completely_inside_obstacle=full_obs and not ct,
        )
        # Keep obstacle-only leaves so the mesh stays connected; pathing
        # penalizes them heavily instead of removing corridors entirely.
        should_subdivide = (
            node.depth < depth
            and (node.contains_target or node.contains_obstacle)
            and node.capacity > 0.55
            and min(node.width, node.height) > max(0.4, 2.0 * pitch)
        )
        if not should_subdivide:
            finished.append(node)
            continue
        # Quad subdivision
        hw, hh = 0.5 * node.width, 0.5 * node.height
        for dx, dy in ((-0.25, -0.25), (0.25, -0.25), (-0.25, 0.25), (0.25, 0.25)):
            unfinished.append(
                CapacityNode(
                    node_id=next_id(),
                    cx=node.cx + dx * node.width,
                    cy=node.cy + dy * node.height,
                    width=hw,
                    height=hh,
                    depth=node.depth + 1,
                    available_layers=node.available_layers,
                    capacity=0.0,
                )
            )

    # Connect adjacent leaves (AABB neighbors, same-layer adjacency)
    edges: list[CapacityEdge] = []
    # Spatial hash for adjacency
    cell = max(0.5, min((n.width for n in finished), default=1.0) * 0.5)
    buckets: dict[tuple[int, int], list[CapacityNode]] = defaultdict(list)
    for n in finished:
        buckets[(int(n.cx // cell), int(n.cy // cell))].append(n)

    def nearby(n: CapacityNode) -> list[CapacityNode]:
        ix, iy = int(n.cx // cell), int(n.cy // cell)
        out: list[CapacityNode] = []
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                out.extend(buckets.get((ix + dx, iy + dy), []))
        return out

    def are_adjacent(a: CapacityNode, b: CapacityNode) -> bool:
        """True if AABBs share a border (or tiny gap) and overlap on the other axis."""
        eps = 0.08 * max(min(a.width, b.width), min(a.height, b.height), 0.2)
        dx = abs(a.cx - b.cx)
        dy = abs(a.cy - b.cy)
        half_w = 0.5 * (a.width + b.width)
        half_h = 0.5 * (a.height + b.height)
        # Touch along X (vertical shared edge) with Y overlap
        touch_x = abs(dx - half_w) <= eps or (dx < half_w and abs(dx - half_w) < max(a.width, b.width) * 0.55)
        touch_y = abs(dy - half_h) <= eps or (dy < half_h and abs(dy - half_h) < max(a.height, b.height) * 0.55)
        overlap_x = dx <= half_w + eps
        overlap_y = dy <= half_h + eps
        # Orthogonal neighbors only (not pure diagonals)
        if overlap_x and overlap_y:
            # Prefer edge-neighbors: not deeply nested
            if dx < half_w * 0.35 and dy < half_h * 0.35:
                return False  # one mostly inside the other
            # edge share: close on one axis
            if dx <= half_w + eps and dy <= 0.55 * max(a.height, b.height):
                return True
            if dy <= half_h + eps and dx <= 0.55 * max(a.width, b.width):
                return True
        return False

    edge_i = 0
    seen: set[tuple[str, str]] = set()
    for a in finished:
        for b in nearby(a):
            if a.node_id >= b.node_id:
                continue
            if not are_adjacent(a, b):
                continue
            key = (a.node_id, b.node_id)
            if key in seen:
                continue
            seen.add(key)
            edge_i += 1
            edges.append(
                CapacityEdge(
                    edge_id=f"ce{edge_i}",
                    a=a.node_id,
                    b=b.node_id,
                    same_layer=True,
                )
            )

    return CapacityMesh(
        nodes=finished,
        edges=edges,
        layers=layers,
        metrics={
            "capacity_depth": depth,
            "effort": effort,
            "board_span_mm": round(span, 3),
            "root_side_mm": round(side, 3),
            "iterations": iters,
            "targets": len(target_pts),
            "via_diameter_mm": via_d,
            "track_pitch_mm": round(pitch, 4),
        },
    )


def _link_python_edges(mesh: CapacityMesh) -> CapacityMesh:
    """Compute adjacency edges for a node list (used after native build)."""
    if not mesh.nodes:
        return mesh
    cell = max(0.5, min(n.width for n in mesh.nodes) * 0.5)
    buckets: dict[tuple[int, int], list[CapacityNode]] = defaultdict(list)
    for n in mesh.nodes:
        buckets[(int(n.cx // cell), int(n.cy // cell))].append(n)

    def nearby(n: CapacityNode) -> list[CapacityNode]:
        ix, iy = int(n.cx // cell), int(n.cy // cell)
        out: list[CapacityNode] = []
        for dx in (-2, -1, 0, 1, 2):
            for dy in (-2, -1, 0, 1, 2):
                out.extend(buckets.get((ix + dx, iy + dy), []))
        return out

    def are_adjacent(a: CapacityNode, b: CapacityNode) -> bool:
        eps = 0.08 * max(min(a.width, b.width), min(a.height, b.height), 0.2)
        dx = abs(a.cx - b.cx)
        dy = abs(a.cy - b.cy)
        half_w = 0.5 * (a.width + b.width)
        half_h = 0.5 * (a.height + b.height)
        if dx < half_w * 0.35 and dy < half_h * 0.35:
            return False
        if dx <= half_w + eps and dy <= 0.55 * max(a.height, b.height):
            return True
        if dy <= half_h + eps and dx <= 0.55 * max(a.width, b.width):
            return True
        return False

    edges: list[CapacityEdge] = []
    edge_i = 0
    seen: set[tuple[str, str]] = set()
    for a in mesh.nodes:
        for b in nearby(a):
            if a.node_id >= b.node_id:
                continue
            if not are_adjacent(a, b):
                continue
            key = (a.node_id, b.node_id)
            if key in seen:
                continue
            seen.add(key)
            edge_i += 1
            edges.append(CapacityEdge(edge_id=f"ce{edge_i}", a=a.node_id, b=b.node_id))
    mesh.edges = edges
    mesh.metrics["edges"] = len(edges)
    return mesh


def _capacity_histogram(nodes: list[CapacityNode]) -> dict[str, int]:
    hist = {"0-0.5": 0, "0.5-1": 0, "1-2": 0, "2-4": 0, "4+": 0}
    for n in nodes:
        c = n.capacity
        if c < 0.5:
            hist["0-0.5"] += 1
        elif c < 1.0:
            hist["0.5-1"] += 1
        elif c < 2.0:
            hist["1-2"] += 1
        elif c < 4.0:
            hist["2-4"] += 1
        else:
            hist["4+"] += 1
    return hist


def path_through_mesh(
    mesh: CapacityMesh,
    start: tuple[float, float],
    goal: tuple[float, float],
    *,
    occupancy: dict[str, float] | None = None,
    history: dict[str, float] | None = None,
) -> list[str]:
    """A* over capacity mesh nodes from start to goal (node ids)."""
    if not mesh.nodes:
        return []
    occupancy = occupancy or {}
    history = history or {}
    # Prefer containing leaf; else nearest center
    def nearest_node(x: float, y: float) -> str | None:
        hit = None
        best = 1e18
        for n in mesh.nodes:
            if n.contains_point(x, y):
                return n.node_id
            d = math.hypot(n.cx - x, n.cy - y)
            if d < best:
                best = d
                hit = n.node_id
        return hit

    start_id = nearest_node(start[0], start[1])
    goal_id = nearest_node(goal[0], goal[1])
    if start_id is None or goal_id is None:
        return []
    if start_id == goal_id:
        return [start_id]

    nodes = mesh.node_map()
    adj = mesh.adjacency()
    import heapq

    open_h: list[tuple[float, str]] = [(0.0, start_id)]
    gscore = {start_id: 0.0}
    came: dict[str, str] = {}
    while open_h:
        _, cur = heapq.heappop(open_h)
        if cur == goal_id:
            path = [cur]
            while cur in came:
                cur = came[cur]
                path.append(cur)
            path.reverse()
            return path
        for nxt in adj.get(cur, []):
            n_cur = nodes[cur]
            n_nxt = nodes[nxt]
            step = math.hypot(n_nxt.cx - n_cur.cx, n_nxt.cy - n_cur.cy)
            step += 2.5 * occupancy.get(nxt, 0.0) / max(0.25, n_nxt.capacity)
            step += history.get(nxt, 0.0)
            if n_nxt.completely_inside_obstacle:
                step += 50.0
            ng = gscore[cur] + step
            if ng + 1e-12 < gscore.get(nxt, 1e18):
                gscore[nxt] = ng
                came[nxt] = cur
                h = math.hypot(n_nxt.cx - nodes[goal_id].cx, n_nxt.cy - nodes[goal_id].cy)
                heapq.heappush(open_h, (ng + h, nxt))
    return []
