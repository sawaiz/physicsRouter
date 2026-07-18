"""Clearance-aware TopoR-style free-angle router (orchestration layer).

The geometric router lives in the C++ core (``pr_native``): exact obstacle map
(spatial hash + Liang–Barsky + painted-copper distance) and the full free-angle
search (LOS / detours / radar / hierarchical A* / rubberband). Python owns net
ordering, via planning, polish, and reporting. There is no Python fallback —
build the core with ``bash scripts/build_native.sh``.
See DESIGN.md for policy decisions (soft fallback, DRC loop).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from physics_router.models import BoardModel, PlacementConfig

# progress_cb(done_nets, total_nets, net_name, stage, detail)
ProgressCallback = Callable[[int, int, str, str, dict[str, Any]], None]

_NATIVE: Any = None


def _native_core() -> Any:
    """Load the required C++ core, searching native/build for dev checkouts."""
    global _NATIVE
    if _NATIVE is not None:
        return _NATIVE
    try:
        import pr_native  # type: ignore[import-not-found]
    except ImportError:
        import sys

        build = Path(__file__).resolve().parents[2] / "native" / "build"
        if build.is_dir():
            sys.path.insert(0, str(build))
        try:
            import pr_native  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "physicsRouter requires the native C++ router core (pr_native). "
                "Build it with: bash scripts/build_native.sh"
            ) from exc
    _NATIVE = pr_native
    return _NATIVE


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
    # Explainable routing: why this via exists
    reason: str = ""
    alternatives_considered: int = 0
    blocked_same_layer: list[str] = field(default_factory=list)


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
                    "reason": v.reason or "",
                    "alternatives_considered": v.alternatives_considered,
                    "blocked_same_layer": list(v.blocked_same_layer or []),
                }
                for v in self.vias
            ],
            "explanations": (self.quality or {}).get("explanations") or {},
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


def _nets_at_ref(board: BoardModel, ref: str) -> list[str]:
    nets: set[str] = set()
    c = board.components.get(ref)
    if c is None:
        return []
    for p in c.pads or []:
        if p.get("net"):
            nets.add(str(p["net"]))
    # also from connectivity table
    for n, pins in board.nets.items():
        if any(r == ref for r, _ in pins):
            nets.add(n)
    return sorted(nets)


def fanout_anchor(
    board: BoardModel,
    ref: str,
    net_name: str,
    *,
    radius_mm: float | None = None,
    pad_num: str | None = None,
) -> tuple[float, float]:
    """Escape / pin point for a net on a footprint.

    Prefer real pad coordinates from the PCB (local pad offset + footprint pose).
    Fall back to angular fanout around multi-net ICs so nets do not share one origin.
    """
    c = board.components[ref]
    # Prefer real pad XY when graphics/pads carry local coordinates
    pads = list(c.pads or [])
    pad_match = None
    if pad_num is not None:
        for p in pads:
            if str(p.get("num")) == str(pad_num):
                pad_match = p
                break
    if pad_match is None:
        for p in pads:
            if str(p.get("net") or "") == net_name:
                pad_match = p
                break
    if pad_match is not None and ("x" in pad_match or "y" in pad_match):
        try:
            from physics_router.kicad_io import local_to_board

            lx = float(pad_match.get("x") or 0.0)
            ly = float(pad_match.get("y") or 0.0)
            return local_to_board(c.x_mm, c.y_mm, c.rotation_deg, lx, ly)
        except Exception:
            pass

    nets = _nets_at_ref(board, ref)
    if len(nets) <= 1:
        return (c.x_mm, c.y_mm)
    r = radius_mm
    if r is None:
        r = max(0.8, 0.55 * max(c.width_mm, c.height_mm, 1.0))
    try:
        i = nets.index(net_name)
    except ValueError:
        i = abs(hash(net_name)) % max(len(nets), 1)
    n = max(len(nets), 1)
    ang = 2.0 * math.pi * (i / n)
    return (c.x_mm + r * math.cos(ang), c.y_mm + r * math.sin(ang))


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


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


def _route_result_from_dict(raw: dict[str, Any]) -> RouteResult:
    """Hydrate RouteResult from native bridge dict."""
    segs = [
        RouteSegment(
            x1=float(s["x1"]),
            y1=float(s["y1"]),
            x2=float(s["x2"]),
            y2=float(s["y2"]),
            layer=str(s.get("layer", "F.Cu")),
            net=str(s.get("net", "")),
            width_mm=float(s.get("width_mm", 0.25)),
        )
        for s in raw.get("segments") or []
    ]
    vias = [
        Via(
            x=float(v["x"]),
            y=float(v["y"]),
            net=str(v.get("net", "")),
            size_mm=float(v.get("size_mm", 0.8)),
            drill_mm=float(v.get("drill_mm", 0.4)),
            layers=tuple(v.get("layers") or ("F.Cu", "B.Cu")),  # type: ignore[arg-type]
            reason=str(v.get("reason") or ""),
            alternatives_considered=int(v.get("alternatives_considered") or 0),
            blocked_same_layer=list(v.get("blocked_same_layer") or []),
        )
        for v in raw.get("vias") or []
    ]
    reports = []
    for nr in raw.get("net_reports") or []:
        reports.append(
            NetRouteReport(
                net=str(nr.get("net", "")),
                pins=int(nr.get("pins") or 0),
                length_mm=float(nr.get("length_mm") or 0),
                segments=int(nr.get("segments") or 0),
                vias=int(nr.get("vias") or 0),
                status=str(nr.get("status") or "ok"),
                method=str(nr.get("method") or ""),
                notes=list(nr.get("notes") or []),
            )
        )
    q = dict(raw.get("quality") or {})
    out = RouteResult(
        segments=segs,
        vias=vias,
        via_count=int(raw.get("via_count") or len(vias)),
        total_length_mm=float(raw.get("total_length_mm") or 0),
        unrouted_nets=list(raw.get("unrouted_nets") or []),
        clearance_violations=int(raw.get("clearance_violations") or 0),
        notes=list(raw.get("notes") or []),
        net_reports=reports,
        quality=q,
    )
    out.compute_quality()
    # keep explanations / backend fields after recompute
    for k in ("explanations", "pipeline", "backend", "si_mfg"):
        if k in q:
            out.quality[k] = q[k]
    return out


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


@dataclass
class PaintedSeg:
    x1: float
    y1: float
    x2: float
    y2: float
    width_mm: float
    net: str


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
        # Python mirrors for topology/elastic/regeometry consumers; the C++
        # ExactMap is the clearance authority for every query.
        self.obstacles: dict[str, list[Obstacle]] = {ly: [] for ly in self.layers}
        self.painted: dict[str, list[PaintedSeg]] = {ly: [] for ly in self.layers}
        self._layer_ids: dict[str, int] = {ly: i for i, ly in enumerate(self.layers)}
        self._net_ids: dict[str, int] = {}
        self._native = _native_core().ExactMap(
            self.x_min, self.x_max, self.y_min, self.y_max, clearance_mm, len(self.layers)
        )

    def _lid(self, layer: str) -> int:
        lid = self._layer_ids.get(layer)
        if lid is None:
            lid = len(self._layer_ids)
            self._layer_ids[layer] = lid
            self.obstacles.setdefault(layer, [])
            self.painted.setdefault(layer, [])
        return lid

    def _nid(self, net: str | None) -> int:
        if net is None:
            return -1
        nid = self._net_ids.get(net)
        if nid is None:
            nid = len(self._net_ids)
            self._net_ids[net] = nid
        return nid

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
        lid = self._lid(layer)
        # Inflate by full clearance on each side (not half) so min gap is clearance
        pad = self.clearance_mm * 2 if inflate else 0.0
        ob = Obstacle(cx=cx, cy=cy, w=w + pad, h=h + pad, net=net, layer=layer)
        self.obstacles[layer].append(ob)
        self._native.add_rect(ob.cx, ob.cy, ob.w, ob.h, lid, self._nid(net))

    def in_bounds(self, x: float, y: float) -> bool:
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max

    def blocked(self, x: float, y: float, layer: str, net: str) -> bool:
        return bool(self._native.blocked(x, y, self._lid(layer), self._nid(net)))

    def segment_blocked(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: str,
        net: str,
        width_mm: float = 0.25,
    ) -> bool:
        """True if candidate segment would violate clearance vs obstacles or copper."""
        return bool(
            self._native.segment_blocked(
                x1, y1, x2, y2, self._lid(layer), self._nid(net), width_mm=width_mm
            )
        )

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
        """Record copper and paint inflated keepout so later nets cannot touch."""
        lid = self._lid(layer)
        self.painted[layer].append(
            PaintedSeg(x1=x1, y1=y1, x2=x2, y2=y2, width_mm=width_mm, net=net)
        )
        self._native.add_painted(x1, y1, x2, y2, lid, width_mm, self._nid(net))
        length = max(_dist((x1, y1), (x2, y2)), 0.01)
        # Keepout diameter = track width + 2*clearance (full gap for next track edge)
        keep = width_mm + 2.0 * self.clearance_mm
        step = max(self.clearance_mm * 0.5, keep * 0.35, 0.12)
        steps = max(1, min(120, int(math.ceil(length / step))))
        for i in range(steps + 1):
            t = i / steps
            px = x1 + (x2 - x1) * t
            py = y1 + (y2 - y1) * t
            # inflate=False: keep already includes clearance
            self.add_rect(px, py, keep, keep, layer, net=net, inflate=False)


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


def _snap(v: float, grid: float) -> float:
    return round(v / grid) * grid


def free_angle_route(
    start: tuple[float, float],
    goal: tuple[float, float],
    layer: str,
    net: str,
    om: ObstacleMap,
    grid_mm: float = 0.1,
    max_expansions: int = 8000,
    *,
    width_mm: float = 0.25,
    method_out: list[str] | None = None,
    congestion: Any | None = None,
) -> list[tuple[float, float]] | None:
    """Free-angle search in the C++ core (the only router implementation).

    LOS → isotropic detours (obstacle corners, bulges, angled offsets, radar
    scan) → 1/2/3-corner chains → hierarchical multi-grid A* (16-dir on fine
    grids) → rubberband. Every candidate edge is clearance-checked against the
    exact obstacle map. If ``method_out`` is provided, appends one of:
    los | detour | detour2 | detour3 | astar.
    """
    n = _native_core()
    cell_mm = 1.0
    keys: list[int] = []
    costs: list[float] = []
    if congestion is not None:
        cell_mm = float(getattr(congestion, "cell_mm", 1.0) or 1.0)
        pw = float(getattr(congestion, "present_weight", 1.0))
        hw = float(getattr(congestion, "historical_weight", 0.35))
        combined: dict[tuple[int, int], float] = {}
        for (ix, iy, ly), v in getattr(congestion, "present", {}).items():
            if ly == layer:
                combined[(ix, iy)] = combined.get((ix, iy), 0.0) + pw * float(v)
        for (ix, iy, ly), v in getattr(congestion, "historical", {}).items():
            if ly == layer:
                combined[(ix, iy)] = combined.get((ix, iy), 0.0) + hw * float(v)
        for (ix, iy), v in combined.items():
            keys.append((ix << 32) ^ (iy & 0xFFFFFFFF))
            costs.append(v)

    res = n.free_angle_route_exact(
        om._native,
        float(start[0]),
        float(start[1]),
        float(goal[0]),
        float(goal[1]),
        om._lid(layer),
        om._nid(net),
        grid_mm=float(grid_mm or 0.1),
        max_expansions=int(max_expansions),
        width_mm=float(width_mm),
        cong_cell_mm=cell_mm,
        cong_keys=keys,
        cong_costs=costs,
    )
    if res is None:
        return None
    pts, method = res
    if method_out is not None:
        method_out.append(str(method))
    return [(float(x), float(y)) for x, y in pts]



def _rubberband(
    path: list[tuple[float, float]],
    layer: str,
    net: str,
    om: ObstacleMap,
    *,
    width_mm: float = 0.25,
) -> list[tuple[float, float]]:
    """LOS shortcutting in the C++ core (Dayan/TopoR rubberband)."""
    if len(path) <= 2:
        return path
    out = _native_core().rubberband_exact(
        om._native,
        [(float(x), float(y)) for x, y in path],
        om._lid(layer),
        om._nid(net),
        width_mm=float(width_mm),
    )
    return [(float(x), float(y)) for x, y in out]


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
    prefer_native: bool = True,
    net_order: list[str] | None = None,
    style: str = "isotropic",
    congestion: Any | None = None,
    k_homotopy: int | dict[str, int] | None = None,
) -> RouteResult:
    """TopoR-inspired clearance-aware free-angle router with per-net feedback.

    ``soft_fallback``: if True, draw a straight segment when search fails (counts
    as clearance_violation — causes overlaps). Default False for clearance mode
    (leave edge unrouted instead of illegal copper); True only for guide_only.

    ``style``: ``isotropic`` (default) — any-angle paths, no preferred H/V.
    ``net_order``: optional explicit paint order (multi-variant search).

    When the C++ extension ``pr_native`` is built, uses the OpenMP/GPU core unless
    ``prefer_native=False`` or ``guide_only=True`` (guide stays in Python for now).
    """
    layers = layers or list(board.copper_layers) or ["F.Cu", "B.Cu"]
    grid = grid_mm if grid_mm is not None else (config.grid_mm if config else 0.5)
    if guide_only:
        clearance_mm = 0.0
    if soft_fallback is None:
        soft_fallback = bool(guide_only)

    # Native C++ isotropic core (v1.1): free-angle detours, multi-site vias,
    # post-rubberband, via minimize. Use when prefer_native and no live progress.
    # Soft-fallback native is OK for guide-like speed; for legal clearance routes
    # native now keeps soft_fallback=False by default.
    if prefer_native and not guide_only and progress_cb is None:
        try:
            from physics_router.native_bridge import (
                available,
                polish_native_with_python,
                route_board_native,
            )

            if available():
                raw = route_board_native(
                    board,
                    config,
                    clearance_mm=float(clearance_mm),
                    grid_mm=float(grid),
                    soft_fallback=bool(soft_fallback),
                    allow_vias=bool(allow_vias),
                    use_gpu=True,
                    isotropic=True,
                    net_order=net_order,
                )
                if raw is not None:
                    if soft_fallback:
                        return _route_result_from_dict(raw)
                    # Legal path: light Python polish (elastic + SI/MFG)
                    try:
                        return polish_native_with_python(
                            board, config, raw, clearance_mm=float(clearance_mm)
                        )
                    except Exception:
                        return _route_result_from_dict(raw)
        except Exception:
            pass

    om = build_obstacle_map(board, clearance_mm=clearance_mm, layers=layers)
    result = RouteResult()
    x0, x1, y0, y1 = om.x_min, om.x_max, om.y_min, om.y_max
    result.notes.append(
        "guide_only"
        if guide_only
        else f"clearance_mm={clearance_mm} style={style} free_angle+via layers={layers}"
    )
    result.notes.append(
        f"extent=[{x0:.2f},{x1:.2f}]×[{y0:.2f},{y1:.2f}] mm (center-origin OK)"
    )
    if style == "isotropic":
        result.notes.append(
            "isotropic: no preferred H/V directions — any-angle LOS/detour/A* (TopoR-style)"
        )

    # Priority: weight, then prefer fewer pins first within same class (less blockage)
    def net_sort_key(n: str) -> tuple:
        pins = len(board.nets.get(n, []))
        return (-_net_priority(config, n), pins, n)

    if net_order:
        # Preserve caller order; append any nets not listed
        seen = set(net_order)
        net_names = [n for n in net_order if n in board.nets]
        net_names.extend(sorted((n for n in board.nets if n not in seen), key=net_sort_key))
    else:
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
                # Real pad XY when available; else angular fanout on multi-net ICs
                anchors.append(fanout_anchor(board, ref, net_name, pad_num=str(pad)))
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
            kh = 1
            if isinstance(k_homotopy, dict):
                kh = int(k_homotopy.get(net_name, 1))
            elif isinstance(k_homotopy, int):
                kh = k_homotopy
            path, vias = _route_point_to_point(
                current,
                nxt,
                net_name,
                om,
                layers=net_layers,
                grid_mm=grid,
                allow_vias=allow_vias and not guide_only,
                width_mm=width,
                method_out=meth,
                congestion=congestion,
                k_homotopy=max(1, kh),
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
                # Live partial geometry so the UI can redraw mid-route
                progress_cb(
                    ni + 1,
                    total_nets,
                    net_name,
                    report.status,
                    {
                        **report.to_dict(),
                        "partial": {
                            "total_length_mm": result.total_length_mm,
                            "via_count": result.via_count,
                            "clearance_violations": result.clearance_violations,
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
                                for s in result.segments
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
                                for v in result.vias
                            ],
                            "unrouted_nets": list(result.unrouted_nets),
                            "net_reports": [r.to_dict() for r in result.net_reports],
                        },
                    },
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


def _seg_seg_min_dist(
    ax1: float,
    ay1: float,
    ax2: float,
    ay2: float,
    bx1: float,
    by1: float,
    bx2: float,
    by2: float,
) -> float:
    """Minimum distance between two finite segments in 2D."""
    # Sample endpoints + closest approach via projection
    d = min(
        _point_seg_dist(ax1, ay1, bx1, by1, bx2, by2),
        _point_seg_dist(ax2, ay2, bx1, by1, bx2, by2),
        _point_seg_dist(bx1, by1, ax1, ay1, ax2, ay2),
        _point_seg_dist(bx2, by2, ax1, ay1, ax2, ay2),
    )
    # Mid-segment samples for near-parallel long runs
    for t in (0.25, 0.5, 0.75):
        d = min(
            d,
            _point_seg_dist(
                ax1 + (ax2 - ax1) * t,
                ay1 + (ay2 - ay1) * t,
                bx1,
                by1,
                bx2,
                by2,
            ),
        )
    return d


def _route_point_to_point(
    start: tuple[float, float],
    goal: tuple[float, float],
    net: str,
    om: ObstacleMap,
    layers: list[str],
    grid_mm: float,
    allow_vias: bool,
    *,
    width_mm: float = 0.25,
    method_out: list[str] | None = None,
    congestion: Any | None = None,
    k_homotopy: int = 1,
) -> tuple[list[tuple[float, float, str]] | None, list[Via]]:
    blocked_layers: list[str] = []
    alts_same = 0

    # Prefer K-homotopy same-layer paths when k_homotopy > 1
    if k_homotopy > 1:
        try:
            from physics_router.homotopy import k_homotopy_paths, pick_best_homotopy

            for layer in layers:
                cands = k_homotopy_paths(
                    start, goal, layer, net, om,
                    k=k_homotopy, grid_mm=grid_mm, width_mm=width_mm,
                    congestion=congestion,
                )
                alts_same += len(cands)
                best = pick_best_homotopy(cands)
                if best is not None:
                    if method_out is not None:
                        method_out.append(f"homotopy_{best.method}")
                    return ([(p[0], p[1], layer) for p in best.points], [])
                blocked_layers.append(layer)
        except Exception:
            pass

    for layer in layers:
        meth: list[str] = []
        poly = free_angle_route(
            start,
            goal,
            layer,
            net,
            om,
            grid_mm=grid_mm,
            width_mm=width_mm,
            method_out=meth,
            congestion=congestion,
        )
        if poly is not None and len(poly) >= 2:
            ok = True
            for i in range(len(poly) - 1):
                if om.segment_blocked(
                    poly[i][0],
                    poly[i][1],
                    poly[i + 1][0],
                    poly[i + 1][1],
                    layer,
                    net,
                    width_mm=width_mm,
                ):
                    ok = False
                    break
            if not ok:
                blocked_layers.append(layer)
                continue
            if method_out is not None:
                method_out.extend(meth or ["los"])
            return ([(p[0], p[1], layer) for p in poly], [])
        blocked_layers.append(layer)

    if not allow_vias or len(layers) < 2:
        return None, []

    # Prefer electrical connectivity: denser via sites than pure via-min
    mx, my = (start[0] + goal[0]) / 2, (start[1] + goal[1]) / 2
    g = max(grid_mm, 0.15)
    sites: list[tuple[float, float]] = [
        (mx, my),
        (start[0], goal[1]),
        (goal[0], start[1]),
        ((2 * start[0] + goal[0]) / 3, (2 * start[1] + goal[1]) / 3),
        ((start[0] + 2 * goal[0]) / 3, (start[1] + 2 * goal[1]) / 3),
    ]
    for k in (2.0, 4.0, 7.0, 11.0):
        sites.extend(
            [
                (mx + k * g, my),
                (mx - k * g, my),
                (mx, my + k * g),
                (mx, my - k * g),
                (mx + k * g, my + k * g),
                (mx - k * g, my - k * g),
            ]
        )
    for k in (2.0, 5.0):
        sites.extend(
            [
                (start[0] + k * g, start[1]),
                (start[0], start[1] + k * g),
                (goal[0] - k * g, goal[1]),
                (goal[0], goal[1] - k * g),
            ]
        )

    pairs: list[tuple[str, str]] = []
    if len(layers) >= 2:
        pairs.append((layers[0], layers[-1]))
        pairs.append((layers[-1], layers[0]))
        if len(layers) > 2:
            pairs.append((layers[0], layers[1]))
            pairs.append((layers[1], layers[0]))

    sites_tried = 0
    for l0, l1 in pairs:
        for vx, vy in sites:
            vx, vy = _snap(vx, g), _snap(vy, g)
            sites_tried += 1
            if not om.in_bounds(vx, vy):
                continue
            if om.blocked(vx, vy, l0, net) or om.blocked(vx, vy, l1, net):
                continue
            p0 = free_angle_route(
                start,
                (vx, vy),
                l0,
                net,
                om,
                grid_mm=grid_mm,
                max_expansions=2500,
                width_mm=width_mm,
                congestion=congestion,
            )
            p1 = free_angle_route(
                (vx, vy),
                goal,
                l1,
                net,
                om,
                grid_mm=grid_mm,
                max_expansions=2500,
                width_mm=width_mm,
                congestion=congestion,
            )
            if p0 and p1:
                if method_out is not None:
                    method_out.append("via")
                path = [(x, y, l0) for x, y in p0] + [(x, y, l1) for x, y in p1[1:]]
                blocked_u = sorted(set(blocked_layers))
                reason = (
                    f"Same-layer path blocked on {', '.join(blocked_u) or 'all tried layers'}; "
                    f"layer transition {l0}→{l1} at ({vx:.2f},{vy:.2f}). "
                    f"Considered {alts_same} same-layer homotopy class(es) and "
                    f"{sites_tried} via site(s)."
                )
                via = Via(
                    x=vx,
                    y=vy,
                    net=net,
                    layers=(l0, l1),
                    reason=reason,
                    alternatives_considered=alts_same + sites_tried,
                    blocked_same_layer=blocked_u,
                )
                return path, [via]
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
    # Paint all copper as obstacles first (same-net later allowed in segment_blocked)
    for s in result.segments:
        om.paint_trace(s.x1, s.y1, s.x2, s.y2, s.layer, s.width_mm, s.net)

    for net, segs in by_net.items():
        width = segs[0].width_mm if segs else 0.25
        # Group into *continuous* polylines per layer (MST edges must not be chained)
        polylines: dict[str, list[list[tuple[float, float]]]] = {}
        for s in segs:
            polylines.setdefault(s.layer, [])
            chains = polylines[s.layer]
            a, b = (s.x1, s.y1), (s.x2, s.y2)
            attached = False
            for chain in chains:
                if chain[-1] == a:
                    chain.append(b)
                    attached = True
                    break
                if chain[-1] == b:
                    chain.append(a)
                    attached = True
                    break
                if chain[0] == a:
                    chain.insert(0, b)
                    attached = True
                    break
                if chain[0] == b:
                    chain.insert(0, a)
                    attached = True
                    break
            if not attached:
                chains.append([a, b])
        for layer, chains in polylines.items():
            for pts in chains:
                cleaned = _rubberband(pts, layer, net, om, width_mm=width)
                for i in range(len(cleaned) - 1):
                    x1, y1 = cleaned[i]
                    x2, y2 = cleaned[i + 1]
                    new_segs.append(
                        RouteSegment(
                            x1=x1,
                            y1=y1,
                            x2=x2,
                            y2=y2,
                            layer=layer,
                            net=net,
                            width_mm=width,
                        )
                    )
                    total += _dist((x1, y1), (x2, y2))

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


def remove_redundant_vias(
    result: RouteResult,
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    clearance_mm: float = 0.2,
    aggressive: bool = False,
) -> RouteResult:
    """Optionally drop vias when stubs can legally merge onto one layer.

    Electrical connectivity and clearance take priority over via count.
    When ``aggressive`` is False (default), keep vias unless the merge is
    clearly free (short stubs, no extra length).
    """
    if not result.vias:
        return result
    if not aggressive:
        # Connectivity-first policy: do not strip vias by default
        result.notes.append("via_policy: keep vias (connectivity/clearance > via-min)")
        return result
    layers = sorted({s.layer for s in result.segments} | set(board.copper_layers or []))
    if len(layers) < 2:
        return result
    om = build_obstacle_map(board, clearance_mm=clearance_mm, layers=layers)
    for s in result.segments:
        om.paint_trace(s.x1, s.y1, s.x2, s.y2, s.layer, s.width_mm, s.net)

    kept_vias: list[Via] = []
    removed = 0
    segs = list(result.segments)

    for via in result.vias:
        # Segments incident to this via (within snap tolerance)
        tol = 0.35
        incident = [
            s
            for s in segs
            if s.net == via.net
            and (
                _dist((s.x1, s.y1), (via.x, via.y)) < tol
                or _dist((s.x2, s.y2), (via.x, via.y)) < tol
            )
        ]
        if len(incident) < 2:
            kept_vias.append(via)
            continue
        # Collect far endpoints on each layer
        ends_by_layer: dict[str, list[tuple[float, float]]] = {}
        for s in incident:
            if _dist((s.x1, s.y1), (via.x, via.y)) < tol:
                ends_by_layer.setdefault(s.layer, []).append((s.x2, s.y2))
            else:
                ends_by_layer.setdefault(s.layer, []).append((s.x1, s.y1))
        # Try to place all ends on a single common layer
        merged = False
        for target_ly in layers:
            ends: list[tuple[float, float]] = []
            for pts in ends_by_layer.values():
                ends.extend(pts)
            if len(ends) < 2:
                continue
            width = incident[0].width_mm
            # Check path between every pair of ends on target layer via via site
            ok = True
            for ex, ey in ends:
                if om.segment_blocked(ex, ey, via.x, via.y, target_ly, via.net, width_mm=width):
                    ok = False
                    break
            if not ok:
                continue
            # Rebuild: remove incident segs, add same-layer stubs
            drop_ids = {id(s) for s in incident}
            segs = [s for s in segs if id(s) not in drop_ids]
            for ex, ey in ends:
                segs.append(
                    RouteSegment(
                        x1=ex,
                        y1=ey,
                        x2=via.x,
                        y2=via.y,
                        layer=target_ly,
                        net=via.net,
                        width_mm=width,
                    )
                )
                om.paint_trace(ex, ey, via.x, via.y, target_ly, width, via.net)
            removed += 1
            merged = True
            break
        if not merged:
            kept_vias.append(via)

    if removed == 0:
        return result

    total = sum(_dist((s.x1, s.y1), (s.x2, s.y2)) for s in segs)
    out = RouteResult(
        segments=segs,
        vias=kept_vias,
        via_count=len(kept_vias),
        total_length_mm=total,
        unrouted_nets=list(result.unrouted_nets),
        clearance_violations=result.clearance_violations,
        notes=list(result.notes)
        + [f"via_minimize: removed {removed} redundant via(s) → {len(kept_vias)} left"],
        net_reports=list(result.net_reports),
    )
    # refresh via counts on reports
    via_by_net: dict[str, int] = {}
    for v in kept_vias:
        via_by_net[v.net] = via_by_net.get(v.net, 0) + 1
    for rep in out.net_reports:
        rep.vias = via_by_net.get(rep.net, 0)
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
