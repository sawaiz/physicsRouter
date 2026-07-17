"""Clearance-aware TopoR-style free-angle topological router.

Strategy:
1. Pad-level obstacle map (foreign nets only), inflated by clearance.
2. Route nets in priority order (critical / high weight first).
3. Free-angle path: direct LOS → 8-direction A* → detour via obstacle corners.
4. Layer change via vias when same-layer path fails.
5. Paint routed copper so later nets keep clearance.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from physics_router.models import BoardModel, PlacementConfig


@dataclass
class RouteSegment:
    x1: float
    y1: float
    x2: float
    y2: float
    layer: str = "F.Cu"
    net: str = ""
    width_mm: float = 0.25


@dataclass
class Via:
    x: float
    y: float
    net: str = ""
    size_mm: float = 0.8
    drill_mm: float = 0.4
    layers: tuple[str, str] = ("F.Cu", "B.Cu")


@dataclass
class RouteResult:
    segments: list[RouteSegment] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    via_count: int = 0
    total_length_mm: float = 0.0
    unrouted_nets: list[str] = field(default_factory=list)
    clearance_violations: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_length_mm": self.total_length_mm,
            "via_count": self.via_count,
            "unrouted_nets": self.unrouted_nets,
            "clearance_violations": self.clearance_violations,
            "notes": self.notes,
            "segments": [
                {
                    "net": s.net,
                    "x1": s.x1,
                    "y1": s.y1,
                    "x2": s.x2,
                    "y2": s.y2,
                    "layer": s.layer,
                    "width_mm": s.width_mm,
                }
                for s in self.segments
            ],
            "vias": [
                {
                    "net": v.net,
                    "x": v.x,
                    "y": v.y,
                    "size_mm": v.size_mm,
                    "drill_mm": v.drill_mm,
                    "layers": list(v.layers),
                }
                for v in self.vias
            ],
        }


def pad_anchor(board: BoardModel, ref: str, _pad: str) -> tuple[float, float]:
    c = board.components[ref]
    return (c.x_mm, c.y_mm)


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _point_in_rect(px: float, py: float, cx: float, cy: float, w: float, h: float) -> bool:
    return abs(px - cx) <= w / 2 and abs(py - cy) <= h / 2


def _segment_hits_rect(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    cx: float,
    cy: float,
    w: float,
    h: float,
    samples: int = 8,
) -> bool:
    for i in range(samples + 1):
        t = i / samples
        px = x1 + (x2 - x1) * t
        py = y1 + (y2 - y1) * t
        if _point_in_rect(px, py, cx, cy, w, h):
            return True
    return False


@dataclass
class Obstacle:
    cx: float
    cy: float
    w: float
    h: float
    net: str | None = None
    layer: str = "F.Cu"

    def corners(self, pad: float = 0.1) -> list[tuple[float, float]]:
        hw, hh = self.w / 2 + pad, self.h / 2 + pad
        return [
            (self.cx - hw, self.cy - hh),
            (self.cx + hw, self.cy - hh),
            (self.cx - hw, self.cy + hh),
            (self.cx + hw, self.cy + hh),
        ]


class ObstacleMap:
    def __init__(
        self,
        width_mm: float,
        height_mm: float,
        layers: list[str] | None = None,
        clearance_mm: float = 0.2,
    ) -> None:
        self.width_mm = width_mm
        self.height_mm = height_mm
        self.layers = layers or ["F.Cu", "B.Cu"]
        self.clearance_mm = clearance_mm
        self.obstacles: dict[str, list[Obstacle]] = {ly: [] for ly in self.layers}

    def add_rect(
        self,
        cx: float,
        cy: float,
        w: float,
        h: float,
        layer: str,
        net: str | None = None,
        inflate: bool = True,
    ) -> None:
        if layer not in self.obstacles:
            self.obstacles[layer] = []
        pad = self.clearance_mm * 2 if inflate else 0.0
        self.obstacles[layer].append(
            Obstacle(cx=cx, cy=cy, w=w + pad, h=h + pad, net=net, layer=layer)
        )

    def in_bounds(self, x: float, y: float) -> bool:
        return 0 <= x <= self.width_mm and 0 <= y <= self.height_mm

    def blocked(self, x: float, y: float, layer: str, net: str) -> bool:
        if not self.in_bounds(x, y):
            return True
        for ob in self.obstacles.get(layer, []):
            if ob.net is not None and ob.net == net:
                continue
            if _point_in_rect(x, y, ob.cx, ob.cy, ob.w, ob.h):
                return True
        return False

    def segment_blocked(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: str,
        net: str,
    ) -> bool:
        if not (self.in_bounds(x1, y1) and self.in_bounds(x2, y2)):
            return True
        for ob in self.obstacles.get(layer, []):
            if ob.net is not None and ob.net == net:
                continue
            if _segment_hits_rect(x1, y1, x2, y2, ob.cx, ob.cy, ob.w, ob.h):
                return True
        return False

    def paint_trace(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: str,
        width_mm: float,
        net: str,
    ) -> None:
        length = max(_dist((x1, y1), (x2, y2)), 0.01)
        steps = max(1, int(math.ceil(length / max(width_mm, 0.4))))
        for i in range(steps + 1):
            t = i / steps
            px = x1 + (x2 - x1) * t
            py = y1 + (y2 - y1) * t
            self.add_rect(px, py, width_mm, width_mm, layer, net=net, inflate=True)


def build_obstacle_map(
    board: BoardModel,
    clearance_mm: float = 0.2,
    layers: list[str] | None = None,
) -> ObstacleMap:
    """Obstacles from pads (per-net), not full courtyards — allows free-angle escape."""
    layers = layers or ["F.Cu", "B.Cu"]
    om = ObstacleMap(board.width_mm, board.height_mm, layers=layers, clearance_mm=clearance_mm)
    for c in board.components.values():
        if c.pads:
            for p in c.pads:
                net = p.get("net")
                # small pad disc at component center (pad offsets unknown in simplified model)
                for ly in layers:
                    om.add_rect(c.x_mm, c.y_mm, 0.8, 0.8, ly, net=str(net) if net else None, inflate=True)
        else:
            for ly in layers:
                om.add_rect(c.x_mm, c.y_mm, 1.0, 1.0, ly, net=None, inflate=True)
    return om


_DIRS8 = [
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (-1, 1),
    (-1, -1),
]


def _snap(v: float, grid: float) -> float:
    return round(v / grid) * grid


def free_angle_route(
    start: tuple[float, float],
    goal: tuple[float, float],
    layer: str,
    net: str,
    om: ObstacleMap,
    grid_mm: float = 0.5,
    max_expansions: int = 4000,
) -> list[tuple[float, float]] | None:
    """Direct LOS, corner detours, then bounded 8-dir A* with rubberband."""
    # 1) straight free-angle
    if not om.segment_blocked(start[0], start[1], goal[0], goal[1], layer, net):
        return [start, goal]

    # 2) single-corner detours around nearby obstacles
    detour_pts: list[tuple[float, float]] = []
    for ob in om.obstacles.get(layer, []):
        if ob.net == net:
            continue
        # only nearby obstacles
        if _dist((ob.cx, ob.cy), start) > 30 and _dist((ob.cx, ob.cy), goal) > 30:
            continue
        detour_pts.extend(ob.corners(pad=grid_mm))
    # mid-board helpers
    detour_pts.extend(
        [
            (start[0], goal[1]),
            (goal[0], start[1]),
            ((start[0] + goal[0]) / 2, (start[1] + goal[1]) / 2),
        ]
    )
    for mid in detour_pts:
        if not om.in_bounds(mid[0], mid[1]):
            continue
        if om.blocked(mid[0], mid[1], layer, net):
            continue
        if om.segment_blocked(start[0], start[1], mid[0], mid[1], layer, net):
            continue
        if om.segment_blocked(mid[0], mid[1], goal[0], goal[1], layer, net):
            continue
        return [start, mid, goal]

    # two-corner simple chain
    for a in detour_pts[:24]:
        if om.blocked(a[0], a[1], layer, net) or not om.in_bounds(a[0], a[1]):
            continue
        if om.segment_blocked(start[0], start[1], a[0], a[1], layer, net):
            continue
        for b in detour_pts[:24]:
            if a == b:
                continue
            if om.blocked(b[0], b[1], layer, net) or not om.in_bounds(b[0], b[1]):
                continue
            if om.segment_blocked(a[0], a[1], b[0], b[1], layer, net):
                continue
            if om.segment_blocked(b[0], b[1], goal[0], goal[1], layer, net):
                continue
            return [start, a, b, goal]

    # 3) bounded A*
    sx, sy = _snap(start[0], grid_mm), _snap(start[1], grid_mm)
    gx, gy = _snap(goal[0], grid_mm), _snap(goal[1], grid_mm)
    start_key = (int(round(sx / grid_mm)), int(round(sy / grid_mm)))
    goal_key = (int(round(gx / grid_mm)), int(round(gy / grid_mm)))

    open_heap: list[tuple[float, float, tuple[int, int]]] = []
    heapq.heappush(open_heap, (_dist((sx, sy), (gx, gy)), 0.0, start_key))
    came: dict[tuple[int, int], tuple[int, int] | None] = {start_key: None}
    gscore: dict[tuple[int, int], float] = {start_key: 0.0}
    pos_of = {start_key: (sx, sy)}
    expansions = 0

    while open_heap and expansions < max_expansions:
        _f, g, key = heapq.heappop(open_heap)
        expansions += 1
        x, y = pos_of[key]
        if key == goal_key or _dist((x, y), (gx, gy)) <= grid_mm * 1.5:
            # reconstruct
            path_keys = [key]
            cur = key
            while came[cur] is not None:
                cur = came[cur]  # type: ignore[assignment]
                path_keys.append(cur)
            path_keys.reverse()
            path = [pos_of[k] for k in path_keys]
            path[0] = start
            path[-1] = goal
            return _rubberband(path, layer, net, om)

        for dx, dy in _DIRS8:
            step = grid_mm * (1.414 if dx and dy else 1.0)
            # normalize diagonal step length already via cost
            nx = _snap(x + dx * grid_mm, grid_mm)
            ny = _snap(y + dy * grid_mm, grid_mm)
            nkey = (int(round(nx / grid_mm)), int(round(ny / grid_mm)))
            if nkey in gscore and gscore[nkey] <= g:
                continue
            if om.segment_blocked(x, y, nx, ny, layer, net):
                continue
            ng = g + step
            if ng + 1e-9 < gscore.get(nkey, float("inf")):
                gscore[nkey] = ng
                came[nkey] = key
                pos_of[nkey] = (nx, ny)
                heapq.heappush(open_heap, (ng + _dist((nx, ny), (gx, gy)), ng, nkey))

    return None


def _rubberband(
    path: list[tuple[float, float]],
    layer: str,
    net: str,
    om: ObstacleMap,
) -> list[tuple[float, float]]:
    if len(path) <= 2:
        return path
    out: list[tuple[float, float]] = [path[0]]
    i = 0
    while i < len(path) - 1:
        j = len(path) - 1
        advanced = False
        while j > i + 1:
            x1, y1 = out[-1]
            x2, y2 = path[j]
            if not om.segment_blocked(x1, y1, x2, y2, layer, net):
                out.append(path[j])
                i = j
                advanced = True
                break
            j -= 1
        if not advanced:
            i += 1
            if i < len(path) and out[-1] != path[i]:
                out.append(path[i])
    if out[-1] != path[-1]:
        out.append(path[-1])
    return out


def _net_width(config: PlacementConfig | None, net: str) -> float:
    if config is None:
        return 0.25
    lab = config.net_by_name().get(net)
    if lab is None:
        return 0.25
    from physics_router.models import NetClass

    if lab.net_class in (NetClass.POWER, NetClass.GROUND):
        return 0.5
    if lab.net_class in (NetClass.DIFFERENTIAL, NetClass.HIGH_SPEED, NetClass.RF):
        return 0.2
    return 0.25


def _net_priority(config: PlacementConfig | None, net: str) -> float:
    if config is None:
        return 1.0
    return config.weight_for_net(net)


def topological_guide_route(
    board: BoardModel,
    config: PlacementConfig | None = None,
    preferred_layer: str = "F.Cu",
) -> RouteResult:
    """Guide router without clearance (MST free-angle chains)."""
    return clearance_aware_route(
        board,
        config,
        layers=[preferred_layer, "B.Cu"],
        clearance_mm=0.0,
        allow_vias=False,
        guide_only=True,
    )


def clearance_aware_route(
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    layers: list[str] | None = None,
    clearance_mm: float = 0.2,
    grid_mm: float | None = None,
    allow_vias: bool = True,
    guide_only: bool = False,
) -> RouteResult:
    """TopoR-inspired clearance-aware free-angle router."""
    layers = layers or ["F.Cu", "B.Cu"]
    grid = grid_mm if grid_mm is not None else (config.grid_mm if config else 0.5)
    if guide_only:
        clearance_mm = 0.0

    om = build_obstacle_map(board, clearance_mm=clearance_mm, layers=layers)
    result = RouteResult()
    result.notes.append(
        "guide_only" if guide_only else f"clearance_mm={clearance_mm} free_angle+via layers={layers}"
    )

    net_names = sorted(board.nets.keys(), key=lambda n: _net_priority(config, n), reverse=True)

    for net_name in net_names:
        pins = board.nets[net_name]
        anchors: list[tuple[float, float]] = []
        for ref, pad in pins:
            if ref in board.components:
                anchors.append(pad_anchor(board, ref, pad))
        uniq: list[tuple[float, float]] = []
        for a in anchors:
            if not any(_dist(a, u) < 0.05 for u in uniq):
                uniq.append(a)
        anchors = uniq
        if len(anchors) < 2:
            result.unrouted_nets.append(net_name)
            continue

        width = _net_width(config, net_name)
        remaining = anchors[1:]
        current = anchors[0]
        failed = False
        while remaining:
            nxt = min(remaining, key=lambda p: _dist(current, p))
            remaining.remove(nxt)
            path, vias = _route_point_to_point(
                current,
                nxt,
                net_name,
                om,
                layers=layers,
                grid_mm=grid,
                allow_vias=allow_vias and not guide_only,
            )
            if path is None:
                # last resort: straight free-angle (record violation)
                path = [(current[0], current[1], layers[0]), (nxt[0], nxt[1], layers[0])]
                vias = []
                result.clearance_violations += 1
            for i in range(len(path) - 1):
                x1, y1, ly1 = path[i]
                x2, y2, ly2 = path[i + 1]
                if ly1 != ly2:
                    continue
                seg = RouteSegment(
                    x1=x1, y1=y1, x2=x2, y2=y2, layer=ly1, net=net_name, width_mm=width
                )
                result.segments.append(seg)
                result.total_length_mm += _dist((x1, y1), (x2, y2))
                if not guide_only:
                    om.paint_trace(x1, y1, x2, y2, ly1, width, net_name)
            for v in vias:
                result.vias.append(v)
                result.via_count += 1
                if not guide_only:
                    for ly in layers:
                        om.add_rect(v.x, v.y, v.size_mm, v.size_mm, ly, net=net_name, inflate=True)
            current = nxt
        if failed:
            result.unrouted_nets.append(net_name)

    return result


def _route_point_to_point(
    start: tuple[float, float],
    goal: tuple[float, float],
    net: str,
    om: ObstacleMap,
    layers: list[str],
    grid_mm: float,
    allow_vias: bool,
) -> tuple[list[tuple[float, float, str]] | None, list[Via]]:
    for layer in layers:
        poly = free_angle_route(start, goal, layer, net, om, grid_mm=grid_mm)
        if poly is not None and len(poly) >= 2:
            return ([(p[0], p[1], layer) for p in poly], [])

    if not allow_vias or len(layers) < 2:
        return None, []

    l0, l1 = layers[0], layers[1]
    mx, my = (start[0] + goal[0]) / 2, (start[1] + goal[1]) / 2
    sites = [
        (mx, my),
        (start[0], goal[1]),
        (goal[0], start[1]),
        (mx + 3, my),
        (mx - 3, my),
        (mx, my + 3),
        (mx, my - 3),
    ]
    for vx, vy in sites:
        vx, vy = _snap(vx, grid_mm), _snap(vy, grid_mm)
        if om.blocked(vx, vy, l0, net) or om.blocked(vx, vy, l1, net):
            continue
        p0 = free_angle_route(start, (vx, vy), l0, net, om, grid_mm=grid_mm)
        p1 = free_angle_route((vx, vy), goal, l1, net, om, grid_mm=grid_mm)
        if p0 and p1:
            path = [(x, y, l0) for x, y in p0] + [(x, y, l1) for x, y in p1[1:]]
            return path, [Via(x=vx, y=vy, net=net, layers=(l0, l1))]
        p0 = free_angle_route(start, (vx, vy), l1, net, om, grid_mm=grid_mm)
        p1 = free_angle_route((vx, vy), goal, l0, net, om, grid_mm=grid_mm)
        if p0 and p1:
            path = [(x, y, l1) for x, y in p0] + [(x, y, l0) for x, y in p1[1:]]
            return path, [Via(x=vx, y=vy, net=net, layers=(l1, l0))]
    return None, []


def append_routes_to_kicad_pcb(
    source_path: str,
    dest_path: str,
    result: RouteResult,
) -> None:
    """Append segment and via S-expressions before the final closing paren."""
    from pathlib import Path

    text = Path(source_path).read_text(encoding="utf-8", errors="replace")
    stripped = text.rstrip()
    if not stripped.endswith(")"):
        raise ValueError("Invalid kicad_pcb: expected trailing ')'")
    body = stripped[:-1]
    chunks = ["\n  (generator_add physics_router_topor)\n"]
    for s in result.segments:
        chunks.append(
            f'  (segment (start {s.x1:.4f} {s.y1:.4f}) (end {s.x2:.4f} {s.y2:.4f}) '
            f'(width {s.width_mm:.4f}) (layer "{s.layer}") (net 0) '
            f"(uuid {_fake_uuid(s)}) )\n"
        )
    for v in result.vias:
        chunks.append(
            f'  (via (at {v.x:.4f} {v.y:.4f}) (size {v.size_mm:.4f}) '
            f'(drill {v.drill_mm:.4f}) (layers "{v.layers[0]}" "{v.layers[1]}") '
            f"(net 0) (uuid {_fake_uuid_via(v)}) )\n"
        )
    Path(dest_path).write_text(body + "".join(chunks) + ")\n", encoding="utf-8")


def _fake_uuid(s: RouteSegment) -> str:
    h = abs(hash((s.x1, s.y1, s.x2, s.y2, s.net, s.layer))) % (16**32)
    hex32 = f"{h:032x}"
    return f'"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"'


def _fake_uuid_via(v: Via) -> str:
    h = abs(hash((v.x, v.y, v.net))) % (16**32)
    hex32 = f"{h:032x}"
    return f'"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"'
