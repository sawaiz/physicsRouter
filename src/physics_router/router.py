"""Clearance-aware TopoR-style free-angle topological router.

Strategy:
1. Pad-level obstacle map (foreign nets only), inflated by clearance.
2. Route nets in priority order (critical / high weight first).
3. Free-angle path: direct LOS → 8-direction A* → detour via obstacle corners.
4. Layer change via vias when same-layer path fails.
5. Paint routed copper so later nets keep clearance.
6. Structured per-net feedback + optional progress callback for live UIs.

Board coordinates may be corner-origin (0..W) or center-origin (HALO-style ±W/2).
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from physics_router.models import BoardModel, PlacementConfig

# progress_cb(done_nets, total_nets, net_name, stage, detail)
ProgressCallback = Callable[[int, int, str, str, dict[str, Any]], None]


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
class NetRouteReport:
    """Feedback for a single net after routing attempt."""

    net: str
    pins: int = 0
    length_mm: float = 0.0
    segments: int = 0
    vias: int = 0
    layers: list[str] = field(default_factory=list)
    status: str = "ok"  # ok | soft_violation | unrouted | skipped
    method: str = ""  # los | detour | astar | via | straight_fallback
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "net": self.net,
            "pins": self.pins,
            "length_mm": round(self.length_mm, 4),
            "segments": self.segments,
            "vias": self.vias,
            "layers": self.layers,
            "status": self.status,
            "method": self.method,
            "notes": self.notes,
        }


@dataclass
class RouteResult:
    segments: list[RouteSegment] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    via_count: int = 0
    total_length_mm: float = 0.0
    unrouted_nets: list[str] = field(default_factory=list)
    clearance_violations: int = 0
    notes: list[str] = field(default_factory=list)
    # Rich feedback
    net_reports: list[NetRouteReport] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        by_layer: dict[str, float] = {}
        for s in self.segments:
            by_layer[s.layer] = by_layer.get(s.layer, 0.0) + _dist((s.x1, s.y1), (s.x2, s.y2))
        return {
            "total_length_mm": self.total_length_mm,
            "via_count": self.via_count,
            "unrouted_nets": self.unrouted_nets,
            "clearance_violations": self.clearance_violations,
            "notes": self.notes,
            "quality": self.quality or self.compute_quality(),
            "length_by_layer_mm": {k: round(v, 4) for k, v in sorted(by_layer.items())},
            "net_reports": [r.to_dict() for r in self.net_reports],
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

    def compute_quality(self) -> dict[str, Any]:
        """0–100 quality score from length, vias, violations, completion."""
        n_nets = max(1, len(self.net_reports) or 1)
        routed = sum(
            1 for r in self.net_reports if r.status in ("ok", "soft_violation", "partial")
        )
        if not self.net_reports:
            routed = max(0, n_nets - len(self.unrouted_nets))
        completion = routed / max(1, len(self.net_reports) or (routed + len(self.unrouted_nets)))
        viol_pen = min(40.0, self.clearance_violations * 4.0)
        via_pen = min(20.0, self.via_count * 0.8)
        unroute_pen = min(40.0, len(self.unrouted_nets) * 8.0)
        score = max(0.0, 100.0 * completion - viol_pen - via_pen - unroute_pen)
        grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 55 else "D" if score >= 35 else "F"
        q = {
            **(self.quality or {}),
            "score": round(score, 1),
            "grade": grade,
            "completion": round(completion, 3),
            "routed_nets": routed,
            "violation_penalty": round(viol_pen, 1),
            "via_penalty": round(via_pen, 1),
            "summary": (
                f"grade {grade} ({score:.0f}/100) · "
                f"{self.clearance_violations} soft viol · {self.via_count} vias · "
                f"{len(self.unrouted_nets)} unrouted · {self.total_length_mm:.1f} mm"
            ),
        }
        self.quality = q
        return q


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


def board_extent(board: BoardModel, margin_mm: float = 2.0) -> tuple[float, float, float, float]:
    """Axis-aligned routing extent (supports center-origin boards like HALO-90)."""
    xs = [c.x_mm for c in board.components.values()]
    ys = [c.y_mm for c in board.components.values()]
    if not xs:
        return 0.0, board.width_mm, 0.0, board.height_mm
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    # Ensure at least nominal board size is covered (corner or center origin)
    span_x = max(x_max - x_min, board.width_mm * 0.5)
    span_y = max(y_max - y_min, board.height_mm * 0.5)
    cx = (x_min + x_max) / 2
    cy = (y_min + y_max) / 2
    half_w = max(span_x / 2, board.width_mm / 2) + margin_mm
    half_h = max(span_y / 2, board.height_mm / 2) + margin_mm
    # If all coords are non-negative, prefer classic 0..W box
    if x_min >= -0.5 and y_min >= -0.5:
        return (
            min(0.0, x_min) - margin_mm * 0.25,
            max(board.width_mm, x_max) + margin_mm,
            min(0.0, y_min) - margin_mm * 0.25,
            max(board.height_mm, y_max) + margin_mm,
        )
    return cx - half_w, cx + half_w, cy - half_h, cy + half_h


class ObstacleMap:
    def __init__(
        self,
        width_mm: float,
        height_mm: float,
        layers: list[str] | None = None,
        clearance_mm: float = 0.2,
        *,
        x_min: float | None = None,
        x_max: float | None = None,
        y_min: float | None = None,
        y_max: float | None = None,
    ) -> None:
        self.width_mm = width_mm
        self.height_mm = height_mm
        self.x_min = 0.0 if x_min is None else x_min
        self.x_max = width_mm if x_max is None else x_max
        self.y_min = 0.0 if y_min is None else y_min
        self.y_max = height_mm if y_max is None else y_max
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
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max

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
        # Adaptive sample count by length
        length = _dist((x1, y1), (x2, y2))
        samples = 4 if length < 2 else (6 if length < 10 else 8)
        for ob in self.obstacles.get(layer, []):
            if ob.net is not None and ob.net == net:
                continue
            if _segment_hits_rect(x1, y1, x2, y2, ob.cx, ob.cy, ob.w, ob.h, samples=samples):
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
        # Coarse paint: keep obstacle count manageable for A*
        step = max(width_mm * 1.5, 0.8)
        steps = max(1, min(40, int(math.ceil(length / step))))
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
    layers = layers or list(board.copper_layers) or ["F.Cu", "B.Cu"]
    x0, x1, y0, y1 = board_extent(board)
    om = ObstacleMap(
        board.width_mm,
        board.height_mm,
        layers=layers,
        clearance_mm=clearance_mm,
        x_min=x0,
        x_max=x1,
        y_min=y0,
        y_max=y1,
    )
    for c in board.components.values():
        # Simplified model has no pad offsets — only single-net footprints get a
        # keepout disc (same-net may pass). Multi-net ICs must not block all nets
        # at the component center (that forced zero legal routes).
        pad_w = max(min(c.width_mm, c.height_mm) * 0.35, 0.4)
        pad_h = pad_w
        if c.pads:
            nets = {str(p.get("net")) for p in c.pads if p.get("net")}
            if len(nets) == 1:
                body_net = next(iter(nets))
                for ly in layers:
                    om.add_rect(c.x_mm, c.y_mm, pad_w, pad_h, ly, net=body_net, inflate=True)
            # multi-net: no static keepout; copper paint from earlier nets is enough
        else:
            for ly in layers:
                om.add_rect(c.x_mm, c.y_mm, pad_w, pad_h, ly, net=None, inflate=True)
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
    max_expansions: int = 2500,
    *,
    method_out: list[str] | None = None,
) -> list[tuple[float, float]] | None:
    """Direct LOS, corner detours, then bounded 8-dir A* with rubberband.

    If ``method_out`` is provided, appends one of: los | detour | detour2 | astar.
    """
    def _set_method(m: str) -> None:
        if method_out is not None:
            method_out.append(m)

    # 1) straight free-angle
    if not om.segment_blocked(start[0], start[1], goal[0], goal[1], layer, net):
        _set_method("los")
        return [start, goal]

    # 2) single-corner detours around nearby obstacles
    reach = max(40.0, _dist(start, goal) * 1.5)
    detour_pts: list[tuple[float, float]] = []
    for ob in om.obstacles.get(layer, []):
        if ob.net == net:
            continue
        if _dist((ob.cx, ob.cy), start) > reach and _dist((ob.cx, ob.cy), goal) > reach:
            continue
        detour_pts.extend(ob.corners(pad=grid_mm * 1.5))
    # L-bends + midpoints + radial offsets
    mx, my = (start[0] + goal[0]) / 2, (start[1] + goal[1]) / 2
    detour_pts.extend(
        [
            (start[0], goal[1]),
            (goal[0], start[1]),
            (mx, my),
            (mx + grid_mm * 4, my),
            (mx - grid_mm * 4, my),
            (mx, my + grid_mm * 4),
            (mx, my - grid_mm * 4),
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
        _set_method("detour")
        return [start, mid, goal]

    # two-corner chain (cap candidates — O(n²) segment checks)
    cand = detour_pts[:16]
    for a in cand:
        if om.blocked(a[0], a[1], layer, net) or not om.in_bounds(a[0], a[1]):
            continue
        if om.segment_blocked(start[0], start[1], a[0], a[1], layer, net):
            continue
        for b in cand:
            if a == b:
                continue
            if om.blocked(b[0], b[1], layer, net) or not om.in_bounds(b[0], b[1]):
                continue
            if om.segment_blocked(a[0], a[1], b[0], b[1], layer, net):
                continue
            if om.segment_blocked(b[0], b[1], goal[0], goal[1], layer, net):
                continue
            _set_method("detour2")
            return [start, a, b, goal]

    # 3) bounded A* (works with negative coords via snap keys)
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
            path_keys = [key]
            cur = key
            while came[cur] is not None:
                cur = came[cur]  # type: ignore[assignment]
                path_keys.append(cur)
            path_keys.reverse()
            path = [pos_of[k] for k in path_keys]
            path[0] = start
            path[-1] = goal
            _set_method("astar")
            return _rubberband(path, layer, net, om)

        for dx, dy in _DIRS8:
            step = grid_mm * (1.414 if dx and dy else 1.0)
            nx = _snap(x + dx * grid_mm, grid_mm)
            ny = _snap(y + dy * grid_mm, grid_mm)
            if not om.in_bounds(nx, ny):
                continue
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
    progress_cb: ProgressCallback | None = None,
    soft_fallback: bool | None = None,
) -> RouteResult:
    """TopoR-inspired clearance-aware free-angle router with per-net feedback.

    ``soft_fallback``: if True, draw a straight segment when search fails (counts
    as clearance_violation — causes overlaps). Default False for clearance mode
    (leave edge unrouted instead of illegal copper); True only for guide_only.
    """
    layers = layers or list(board.copper_layers) or ["F.Cu", "B.Cu"]
    grid = grid_mm if grid_mm is not None else (config.grid_mm if config else 0.5)
    if guide_only:
        clearance_mm = 0.0
    if soft_fallback is None:
        soft_fallback = bool(guide_only)

    om = build_obstacle_map(board, clearance_mm=clearance_mm, layers=layers)
    result = RouteResult()
    x0, x1, y0, y1 = om.x_min, om.x_max, om.y_min, om.y_max
    result.notes.append(
        "guide_only"
        if guide_only
        else f"clearance_mm={clearance_mm} free_angle+via layers={layers}"
    )
    result.notes.append(
        f"extent=[{x0:.2f},{x1:.2f}]×[{y0:.2f},{y1:.2f}] mm (center-origin OK)"
    )

    # Priority: weight, then prefer fewer pins first within same class (less blockage)
    def net_sort_key(n: str) -> tuple:
        pins = len(board.nets.get(n, []))
        return (-_net_priority(config, n), pins, n)

    net_names = sorted(board.nets.keys(), key=net_sort_key)
    total_nets = len(net_names)

    for ni, net_name in enumerate(net_names):
        pins = board.nets[net_name]
        report = NetRouteReport(net=net_name, pins=len(pins))
        if progress_cb:
            try:
                progress_cb(
                    ni,
                    total_nets,
                    net_name,
                    "routing",
                    {"pins": len(pins), "priority": _net_priority(config, net_name)},
                )
            except Exception:
                pass

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
            report.status = "skipped"
            report.notes.append("fewer than 2 unique anchors")
            result.net_reports.append(report)
            continue

        width = _net_width(config, net_name)
        # Prefer preferred layers for power/ground (inner if available)
        net_layers = list(layers)
        if config:
            lab = config.net_by_name().get(net_name)
            if lab is not None:
                from physics_router.models import NetClass

                if lab.net_class in (NetClass.POWER, NetClass.GROUND) and len(layers) >= 3:
                    # inners first for planes-ish, then outer
                    inners = [ly for ly in layers if ly.startswith("In")]
                    outers = [ly for ly in layers if ly not in inners]
                    net_layers = inners + outers if inners else layers
                elif lab.net_class in (NetClass.HIGH_SPEED, NetClass.ANALOG) and layers:
                    # outer first
                    outers = [ly for ly in layers if ly.startswith("F.") or ly.startswith("B.")]
                    rest = [ly for ly in layers if ly not in outers]
                    net_layers = outers + rest if outers else layers

        # Prim MST growth from first pin
        remaining = set(range(1, len(anchors)))
        tree = {0}
        methods: list[str] = []
        net_vias = 0
        net_len = 0.0
        net_segs = 0
        layer_set: set[str] = set()
        soft = 0

        while remaining:
            best: tuple[float, int, int] | None = None
            for i in tree:
                for j in remaining:
                    d = _dist(anchors[i], anchors[j])
                    if best is None or d < best[0]:
                        best = (d, i, j)
            assert best is not None
            _, ia, ib = best
            remaining.remove(ib)
            tree.add(ib)
            current, nxt = anchors[ia], anchors[ib]

            meth: list[str] = []
            path, vias = _route_point_to_point(
                current,
                nxt,
                net_name,
                om,
                layers=net_layers,
                grid_mm=grid,
                allow_vias=allow_vias and not guide_only,
                method_out=meth,
            )
            if path is None:
                if soft_fallback:
                    # Illegal copper — only for guide preview; pollutes DRC
                    path = [
                        (current[0], current[1], net_layers[0]),
                        (nxt[0], nxt[1], net_layers[0]),
                    ]
                    vias = []
                    meth = ["straight_fallback"]
                    result.clearance_violations += 1
                    soft += 1
                else:
                    # Prefer open connection over overlapping copper
                    methods.append("unrouted_edge")
                    report.notes.append(
                        f"open edge pins~{ia}-{ib} (no legal path; not drawn)"
                    )
                    continue

            methods.extend(meth)
            for i in range(len(path) - 1):
                x1, y1, ly1 = path[i]
                x2, y2, ly2 = path[i + 1]
                if ly1 != ly2:
                    continue
                seg = RouteSegment(
                    x1=x1, y1=y1, x2=x2, y2=y2, layer=ly1, net=net_name, width_mm=width
                )
                result.segments.append(seg)
                d = _dist((x1, y1), (x2, y2))
                result.total_length_mm += d
                net_len += d
                net_segs += 1
                layer_set.add(ly1)
                if not guide_only:
                    om.paint_trace(x1, y1, x2, y2, ly1, width, net_name)
            for v in vias:
                result.vias.append(v)
                result.via_count += 1
                net_vias += 1
                if not guide_only:
                    for ly in layers:
                        om.add_rect(v.x, v.y, v.size_mm, v.size_mm, ly, net=net_name, inflate=True)

        report.length_mm = net_len
        report.segments = net_segs
        report.vias = net_vias
        report.layers = sorted(layer_set)
        report.method = "+".join(dict.fromkeys(methods)) if methods else "none"
        open_edges = methods.count("unrouted_edge")
        if soft:
            report.status = "soft_violation"
            report.notes.append(f"{soft} straight fallback(s)")
        elif open_edges and net_segs == 0:
            report.status = "unrouted"
            if net_name not in result.unrouted_nets:
                result.unrouted_nets.append(net_name)
        elif open_edges:
            report.status = "partial"
            report.notes.append(f"{open_edges} open edge(s)")
        else:
            report.status = "ok"
        result.net_reports.append(report)

        if progress_cb:
            try:
                progress_cb(
                    ni + 1,
                    total_nets,
                    net_name,
                    report.status,
                    report.to_dict(),
                )
            except Exception:
                pass

    # CPX length match feedback
    cpx = [r for r in result.net_reports if r.net.upper().startswith("CPX") and r.length_mm > 0]
    if len(cpx) >= 2:
        lengths = [r.length_mm for r in cpx]
        avg = sum(lengths) / len(lengths)
        skew = max(lengths) - min(lengths)
        result.notes.append(
            f"cpx_match: n={len(cpx)} avg={avg:.2f}mm skew={skew:.2f}mm "
            f"({'good' if skew < avg * 0.25 else 'high skew — consider bundle reorder'})"
        )

    if not guide_only:
        audit = audit_same_layer_clearance(result, clearance_mm=clearance_mm)
        result.clearance_violations += int(audit.get("near_miss_pairs", 0))
        if audit.get("notes"):
            result.notes.extend(audit["notes"][:8])
        result.quality = {**(result.quality or {}), "clearance_audit": audit}

    result.compute_quality()
    result.notes.append(result.quality.get("summary", ""))
    return result


def audit_same_layer_clearance(
    result: RouteResult,
    *,
    clearance_mm: float = 0.2,
    sample_step_mm: float = 0.4,
) -> dict[str, Any]:
    """Cheap post-route check: foreign-net segments too close on the same layer."""
    by_layer: dict[str, list[RouteSegment]] = {}
    for s in result.segments:
        by_layer.setdefault(s.layer, []).append(s)

    near = 0
    samples: list[str] = []
    min_sep = clearance_mm

    def _pts(s: RouteSegment) -> list[tuple[float, float]]:
        length = max(_dist((s.x1, s.y1), (s.x2, s.y2)), 0.01)
        n = max(1, int(length / sample_step_mm))
        return [
            (s.x1 + (s.x2 - s.x1) * i / n, s.y1 + (s.y2 - s.y1) * i / n) for i in range(n + 1)
        ]

    for layer, segs in by_layer.items():
        # O(n²) but n is modest for synthetic / matrix bundles
        for i, a in enumerate(segs):
            pa = _pts(a)
            for b in segs[i + 1 :]:
                if a.net == b.net:
                    continue
                for p in pa:
                    # distance to segment b
                    d = _point_seg_dist(p[0], p[1], b.x1, b.y1, b.x2, b.y2)
                    need = min_sep + 0.5 * (a.width_mm + b.width_mm)
                    if d < need:
                        near += 1
                        if len(samples) < 12:
                            samples.append(
                                f"{layer}: {a.net}≈{b.net} d={d:.3f}<{need:.3f}"
                            )
                        break

    notes = []
    if near:
        notes.append(f"clearance_audit: {near} near-miss pair sample(s) @ ≥{min_sep}mm")
        notes.extend(samples[:5])
    else:
        notes.append(f"clearance_audit: OK (no foreign near-miss @ {min_sep}mm)")
    return {"near_miss_pairs": near, "samples": samples, "notes": notes}


def _point_seg_dist(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return _dist((px, py), (x1, y1))
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return _dist((px, py), (x1 + t * dx, y1 + t * dy))


def _route_point_to_point(
    start: tuple[float, float],
    goal: tuple[float, float],
    net: str,
    om: ObstacleMap,
    layers: list[str],
    grid_mm: float,
    allow_vias: bool,
    method_out: list[str] | None = None,
) -> tuple[list[tuple[float, float, str]] | None, list[Via]]:
    for layer in layers:
        meth: list[str] = []
        poly = free_angle_route(
            start, goal, layer, net, om, grid_mm=grid_mm, method_out=meth
        )
        if poly is not None and len(poly) >= 2:
            if method_out is not None:
                method_out.extend(meth or ["los"])
            return ([(p[0], p[1], layer) for p in poly], [])

    if not allow_vias or len(layers) < 2:
        return None, []

    mx, my = (start[0] + goal[0]) / 2, (start[1] + goal[1]) / 2
    # Limited via sites (dense grid was too slow for A*)
    g = max(grid_mm, 0.3)
    sites: list[tuple[float, float]] = [
        (mx, my),
        (start[0], goal[1]),
        (goal[0], start[1]),
        (mx + 3 * g, my),
        (mx - 3 * g, my),
        (mx, my + 3 * g),
        (mx, my - 3 * g),
        (mx + 5 * g, my + 5 * g),
        (mx - 5 * g, my - 5 * g),
    ]

    # Prefer outer layer pairs first, then first↔last through
    pairs: list[tuple[str, str]] = []
    if len(layers) >= 2:
        pairs.append((layers[0], layers[-1]))
        pairs.append((layers[-1], layers[0]))
        if len(layers) > 2:
            pairs.append((layers[0], layers[1]))
            pairs.append((layers[1], layers[0]))

    for l0, l1 in pairs:
        for vx, vy in sites:
            vx, vy = _snap(vx, g), _snap(vy, g)
            if not om.in_bounds(vx, vy):
                continue
            if om.blocked(vx, vy, l0, net) or om.blocked(vx, vy, l1, net):
                continue
            p0 = free_angle_route(
                start, (vx, vy), l0, net, om, grid_mm=grid_mm, max_expansions=2500
            )
            p1 = free_angle_route(
                (vx, vy), goal, l1, net, om, grid_mm=grid_mm, max_expansions=2500
            )
            if p0 and p1:
                if method_out is not None:
                    method_out.append("via")
                path = [(x, y, l0) for x, y in p0] + [(x, y, l1) for x, y in p1[1:]]
                return path, [Via(x=vx, y=vy, net=net, layers=(l0, l1))]
    return None, []


def rubberband_cleanup(
    result: RouteResult,
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    clearance_mm: float = 0.15,
) -> RouteResult:
    """Post-process routes: collapse collinear free-angle segments under clearance.

    Classic Dayan-style improvement: keep topology, shorten geometry. Rebuilds
    obstacle map from board + already-accepted copper so cleanup stays legal.
    """
    layers = sorted({s.layer for s in result.segments}) or ["F.Cu", "B.Cu"]
    om = build_obstacle_map(board, clearance_mm=clearance_mm, layers=layers)
    # Paint foreign nets first (preserve sequential paint order roughly by net)
    by_net: dict[str, list[RouteSegment]] = {}
    for s in result.segments:
        by_net.setdefault(s.net, []).append(s)

    new_segs: list[RouteSegment] = []
    total = 0.0
    for net, segs in by_net.items():
        # Build polylines per layer
        layer_pts: dict[str, list[tuple[float, float]]] = {}
        for s in segs:
            layer_pts.setdefault(s.layer, [])
            pts = layer_pts[s.layer]
            if not pts or pts[-1] != (s.x1, s.y1):
                pts.append((s.x1, s.y1))
            pts.append((s.x2, s.y2))
        width = segs[0].width_mm if segs else 0.25
        for layer, pts in layer_pts.items():
            cleaned = _rubberband(pts, layer, net, om)
            for i in range(len(cleaned) - 1):
                x1, y1 = cleaned[i]
                x2, y2 = cleaned[i + 1]
                new_segs.append(
                    RouteSegment(
                        x1=x1, y1=y1, x2=x2, y2=y2, layer=layer, net=net, width_mm=width
                    )
                )
                total += _dist((x1, y1), (x2, y2))
                om.paint_trace(x1, y1, x2, y2, layer, width, net)

    out = RouteResult(
        segments=new_segs,
        vias=list(result.vias),
        via_count=result.via_count,
        total_length_mm=total,
        unrouted_nets=list(result.unrouted_nets),
        clearance_violations=result.clearance_violations,
        notes=list(result.notes) + [f"rubberband_cleanup segs {len(result.segments)}→{len(new_segs)}"],
        net_reports=list(result.net_reports),
    )
    # refresh per-net lengths after cleanup
    by_net_len: dict[str, float] = {}
    by_net_seg: dict[str, int] = {}
    for s in new_segs:
        by_net_len[s.net] = by_net_len.get(s.net, 0.0) + _dist((s.x1, s.y1), (s.x2, s.y2))
        by_net_seg[s.net] = by_net_seg.get(s.net, 0) + 1
    for rep in out.net_reports:
        if rep.net in by_net_len:
            rep.length_mm = by_net_len[rep.net]
            rep.segments = by_net_seg.get(rep.net, rep.segments)
    out.compute_quality()
    return out


_PR_BEGIN = "  (generator_add physics_router_topor)\n"
_PR_END = "  (generator_add physics_router_topor_end)\n"


def strip_physics_router_copper(text: str) -> str:
    """Remove a previous physicsRouter segment/via block if present."""
    begin = text.find(_PR_BEGIN.strip())
    end_marker = "physics_router_topor_end"
    if begin < 0:
        # legacy: only begin marker without end — drop from begin to last via/segment we can't safely strip
        return text
    end = text.find(end_marker, begin)
    if end < 0:
        return text
    # find end of that s-expr line
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    return text[:begin] + text[line_end + 1 :]


def append_routes_to_kicad_pcb(
    source_path: str,
    dest_path: str,
    result: RouteResult,
    *,
    replace_previous: bool = True,
) -> Path:
    """Append segment and via S-expressions before the final closing paren.

    When ``replace_previous`` is True, strips an earlier physicsRouter copper
    block so re-applying a different variant does not stack copper.
    """
    from pathlib import Path

    src = Path(source_path)
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8", errors="replace")
    if replace_previous:
        text = strip_physics_router_copper(text)
    stripped = text.rstrip()
    if not stripped.endswith(")"):
        raise ValueError("Invalid kicad_pcb: expected trailing ')'")
    body = stripped[:-1]
    chunks = ["\n", _PR_BEGIN]
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
    chunks.append(_PR_END)
    dest.write_text(body + "".join(chunks) + ")\n", encoding="utf-8")
    return dest


def _fake_uuid(s: RouteSegment) -> str:
    h = abs(hash((s.x1, s.y1, s.x2, s.y2, s.net, s.layer))) % (16**32)
    hex32 = f"{h:032x}"
    return f'"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"'


def _fake_uuid_via(v: Via) -> str:
    h = abs(hash((v.x, v.y, v.net))) % (16**32)
    hex32 = f"{h:032x}"
    return f'"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"'
