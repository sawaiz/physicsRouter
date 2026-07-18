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
    # Include Edge.Cuts outline so AABB covers teardrop / non-rect boards
    for g in board.outline or []:
        if g.get("kind") == "circle":
            cx, cy, r = float(g.get("cx") or 0), float(g.get("cy") or 0), float(g.get("r") or 0)
            xs.extend([cx - r, cx + r])
            ys.extend([cy - r, cy + r])
        for p in g.get("pts") or []:
            if len(p) >= 2:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
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


def outline_polygon_from_board(
    board: BoardModel, *, circle_samples: int = 64
) -> list[tuple[float, float]] | None:
    """Build a closed Edge.Cuts polygon for routing bounds (or None).

    Prefers stitching open Edge.Cuts arc polylines into one ring (HALO teardrop).
    Falls back to sampling a disk circle when that is all we have.
    """
    outline = list(board.outline or [])
    if not outline:
        return None

    def _close(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(pts) < 3:
            return pts
        if _dist(pts[0], pts[-1]) > 1e-6:
            pts = pts + [pts[0]]
        return pts

    # Closed polys first
    closed_polys: list[list[tuple[float, float]]] = []
    open_polys: list[list[tuple[float, float]]] = []
    circles: list[dict] = []
    for g in outline:
        kind = g.get("kind")
        if kind == "circle" and float(g.get("r") or 0) > 0.5:
            circles.append(g)
        elif kind == "poly" and g.get("pts"):
            pts = [(float(p[0]), float(p[1])) for p in g["pts"] if len(p) >= 2]
            if len(pts) < 2:
                continue
            if g.get("closed") or _dist(pts[0], pts[-1]) < 0.05:
                closed_polys.append(_close(pts[:-1] if _dist(pts[0], pts[-1]) < 0.05 else pts))
            else:
                open_polys.append(pts)
        elif kind == "line":
            open_polys.append(
                [(float(g["x1"]), float(g["y1"])), (float(g["x2"]), float(g["y2"]))]
            )

    # Stitch open polylines by endpoint proximity into rings
    if open_polys:
        unused = list(open_polys)
        chains: list[list[tuple[float, float]]] = []
        while unused:
            chain = list(unused.pop(0))
            progressed = True
            while progressed:
                progressed = False
                for i, poly in enumerate(unused):
                    tol = 0.08
                    if _dist(chain[-1], poly[0]) < tol:
                        chain.extend(poly[1:])
                        unused.pop(i)
                        progressed = True
                        break
                    if _dist(chain[-1], poly[-1]) < tol:
                        chain.extend(reversed(poly[:-1]))
                        unused.pop(i)
                        progressed = True
                        break
                    if _dist(chain[0], poly[-1]) < tol:
                        chain = poly[:-1] + chain
                        unused.pop(i)
                        progressed = True
                        break
                    if _dist(chain[0], poly[0]) < tol:
                        chain = list(reversed(poly[1:])) + chain
                        unused.pop(i)
                        progressed = True
                        break
            chains.append(chain)
        # Prefer closed chains (endpoints meet)
        for ch in sorted(chains, key=len, reverse=True):
            if len(ch) >= 8 and _dist(ch[0], ch[-1]) < 0.15:
                return _close(ch)
            if len(ch) >= 16:
                # Near-closed HALO ring: force close
                return _close(ch)

    if closed_polys:
        best = max(closed_polys, key=len)
        if len(best) >= 3:
            return best

    if circles:
        g = max(circles, key=lambda c: float(c.get("r") or 0))
        cx, cy, r = float(g["cx"]), float(g["cy"]), float(g["r"])
        n = max(16, circle_samples)
        pts = [
            (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
            for i in range(n)
        ]
        return _close(pts)
    return None


def point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    """Even-odd point-in-polygon (poly may be open or closed)."""
    if len(poly) < 3:
        return True
    pts = poly
    if _dist(pts[0], pts[-1]) > 1e-9:
        pts = list(pts) + [pts[0]]
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) + 1e-30) + xi
        ):
            inside = not inside
        j = i
    return inside


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
        outline: list[tuple[float, float]] | None = None,
    ) -> None:
        self.width_mm = width_mm
        self.height_mm = height_mm
        self.x_min = 0.0 if x_min is None else x_min
        self.x_max = width_mm if x_max is None else x_max
        self.y_min = 0.0 if y_min is None else y_min
        self.y_max = height_mm if y_max is None else y_max
        self.layers = layers or ["F.Cu", "B.Cu"]
        self.clearance_mm = clearance_mm
        # Optional Edge.Cuts ring (board mm); rejects free-angle detours outside the PCB
        self.outline: list[tuple[float, float]] | None = None
        # Python mirrors for topology/elastic/regeometry consumers; the C++
        # ExactMap is the clearance authority for every query.
        self.obstacles: dict[str, list[Obstacle]] = {ly: [] for ly in self.layers}
        self.painted: dict[str, list[PaintedSeg]] = {ly: [] for ly in self.layers}
        self._layer_ids: dict[str, int] = {ly: i for i, ly in enumerate(self.layers)}
        self._net_ids: dict[str, int] = {}
        self._native = _native_core().ExactMap(
            self.x_min, self.x_max, self.y_min, self.y_max, clearance_mm, len(self.layers)
        )
        if outline:
            self.set_outline(outline)

    def set_outline(self, pts: list[tuple[float, float]] | None) -> None:
        """Set Edge.Cuts polygon (closed ring). Empty/None clears."""
        if not pts or len(pts) < 3:
            self.outline = None
            if hasattr(self._native, "set_outline"):
                self._native.set_outline([])
            return
        self.outline = [(float(p[0]), float(p[1])) for p in pts]
        if hasattr(self._native, "set_outline"):
            self._native.set_outline(self.outline)

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
        """AABB + optional Edge.Cuts outline (delegates to native when present)."""
        if hasattr(self._native, "in_bounds"):
            return bool(self._native.in_bounds(x, y))
        if not (self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max):
            return False
        if self.outline and not point_in_polygon(x, y, self.outline):
            return False
        return True

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
    """Obstacles from pads (per-net), not full courtyards — allows free-angle escape.

    When ``board.outline`` is present (Edge.Cuts), the map also enforces
    point/segment-in-polygon bounds so routes cannot leave the PCB silhouette.
    """
    layers = layers or list(board.copper_layers) or ["F.Cu", "B.Cu"]
    x0, x1, y0, y1 = board_extent(board)
    poly = outline_polygon_from_board(board)
    om = ObstacleMap(
        board.width_mm,
        board.height_mm,
        layers=layers,
        clearance_mm=clearance_mm,
        x_min=x0,
        x_max=x1,
        y_min=y0,
        y_max=y1,
        outline=poly,
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


def _net_width(
    config: PlacementConfig | None,
    net: str,
    *,
    design_rules: Any | None = None,
) -> float:
    """Track width from design rules when available, else net-class heuristics."""
    if design_rules is not None:
        try:
            return float(design_rules.track_width_for_net(net, config))
        except Exception:
            pass
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
    nets_filter: list[str] | None = None,
    seed_result: RouteResult | None = None,
    style: str = "isotropic",
    congestion: Any | None = None,
    k_homotopy: int | dict[str, int] | None = None,
    design_rules: Any | None = None,
    skip_hybrid: bool = False,
) -> RouteResult:
    """TopoR-inspired clearance-aware free-angle router with per-net feedback.

    ``soft_fallback``: if True, draw a straight segment when search fails (counts
    as clearance_violation — causes overlaps). Default False for clearance mode
    (leave edge unrouted instead of illegal copper); True only for guide_only.

    ``style``: ``isotropic`` (default) — any-angle paths, no preferred H/V.
    ``ring`` / ``halo`` — concentric track + radial (charlieplex LED rings).
    ``auto`` / ``hybrid`` — multi-strategy hybrid (ring + power + critical + general).
    ``nets_filter``: if set, only these nets are routed (hybrid buckets).
    ``seed_result``: prior copper painted as obstacles (other strategies already done).
    ``design_rules``: optional DesignRules for per-net width / clearance floors.
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

    style_l = (style or "isotropic").lower()

    # Hybrid multi-strategy (default for auto): region/net classifier
    if (
        not guide_only
        and not skip_hybrid
        and nets_filter is None
        and seed_result is None
        and style_l in ("auto", "hybrid")
    ):
        try:
            from physics_router.hybrid_route import hybrid_route

            return hybrid_route(
                board,
                config,
                design_rules,
                clearance_mm=float(clearance_mm),
                progress_cb=progress_cb,
            )
        except Exception:
            pass

    # Explicit ring-only path
    if not guide_only and style_l in ("ring", "halo", "halo_ring"):
        try:
            from physics_router.halo_ring import detect_led_ring, halo_ring_route

            if detect_led_ring(board) is not None:
                return halo_ring_route(
                    board,
                    config,
                    clearance_mm=float(clearance_mm),
                    progress_cb=progress_cb,
                )
        except Exception:
            pass

    # Native C++ isotropic core (v1.1): free-angle detours, multi-site vias,
    # post-rubberband, via minimize. Use when prefer_native and no live progress.
    # Soft-fallback native is OK for guide-like speed; for legal clearance routes
    # native now keeps soft_fallback=False by default.
    # Skip native when seeding prior copper or filtering nets (Python path owns paint).
    if (
        prefer_native
        and not guide_only
        and progress_cb is None
        and seed_result is None
        and nets_filter is None
    ):
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
                        result = polish_native_with_python(
                            board, config, raw, clearance_mm=float(clearance_mm)
                        )
                    except Exception:
                        result = _route_result_from_dict(raw)
                    # Same post-pass as Python path: rip-up/repair + honest DRC
                    result = repair_drc_conflicts(
                        result,
                        board,
                        config,
                        clearance_mm=clearance_mm,
                        grid_mm=grid,
                        layers=layers,
                        allow_vias=allow_vias,
                        max_rounds=4,
                    )
                    result = purge_shorting_copper(
                        result, board, config, clearance_mm=clearance_mm
                    )
                    attach_router_drc(result, clearance_mm=clearance_mm, board=board)
                    result.compute_quality()
                    result.notes.append(result.quality.get("summary", ""))
                    return result
        except Exception:
            pass

    om = build_obstacle_map(board, clearance_mm=clearance_mm, layers=layers)
    result = RouteResult()
    # Seed copper from prior hybrid phases as foreign-net obstacles
    if seed_result is not None:
        for s in seed_result.segments:
            om.paint_trace(s.x1, s.y1, s.x2, s.y2, s.layer, s.width_mm, s.net)
        for v in seed_result.vias:
            for ly in layers:
                om.add_rect(v.x, v.y, v.size_mm, v.size_mm, ly, net=v.net, inflate=True)
        result.notes.append(
            f"seed: {len(seed_result.segments)} segs + {len(seed_result.vias)} vias as obstacles"
        )
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

    allowed = set(nets_filter) if nets_filter is not None else None
    if net_order:
        # Preserve caller order; optionally only listed nets (exclusive filter)
        seen = set(net_order)
        net_names = [n for n in net_order if n in board.nets]
        if allowed is None:
            net_names.extend(
                sorted((n for n in board.nets if n not in seen), key=net_sort_key)
            )
    else:
        net_names = sorted(board.nets.keys(), key=net_sort_key)
    if allowed is not None:
        net_names = [n for n in net_names if n in allowed]
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

        width = _net_width(config, net_name, design_rules=design_rules)
        # Prefer preferred layers for power/ground (inner if available)
        # Charlieplex / matrix: stripe across copper layers to cut same-layer crossings
        net_layers = _cpx_layer_order(net_name, layers)
        if design_rules is not None:
            try:
                prefs = list(design_rules.layers_for_net(net_name, config))
                if prefs:
                    # Keep only layers present on this board, preserve preference order
                    net_layers = [ly for ly in prefs if ly in layers] or net_layers
            except Exception:
                pass
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
        # Rip-up nets that still short after sequential paint (dense CPX, etc.)
        result = repair_drc_conflicts(
            result,
            board,
            config,
            clearance_mm=clearance_mm,
            grid_mm=grid,
            layers=layers,
            allow_vias=allow_vias,
            max_rounds=5,
        )
        # Last resort: drop lower-priority copper that still shorts (open > short)
        result = purge_shorting_copper(
            result, board, config, clearance_mm=clearance_mm
        )
        # Built-in router DRC (always on): shorts / spacing / vias / outline
        attach_router_drc(result, clearance_mm=clearance_mm, board=board)

    result.compute_quality()
    result.notes.append(result.quality.get("summary", ""))
    return result


def _cpx_layer_order(net: str, layers: list[str]) -> list[str]:
    """Stripe charlieplex/matrix nets across copper layers to reduce crossings."""
    if not layers:
        return layers
    if not net.upper().startswith("CPX") or len(layers) < 2:
        return list(layers)
    try:
        idx = int("".join(ch for ch in net if ch.isdigit()) or "0")
    except ValueError:
        idx = abs(hash(net)) % len(layers)
    primary = layers[idx % len(layers)]
    return [primary] + [ly for ly in layers if ly != primary]


def _rebuild_totals(r: RouteResult) -> None:
    r.total_length_mm = sum(_dist((s.x1, s.y1), (s.x2, s.y2)) for s in r.segments)
    r.via_count = len(r.vias)


def _reroute_net_into(
    trial: RouteResult,
    net: str,
    board: BoardModel,
    config: PlacementConfig | None,
    *,
    om: ObstacleMap,
    layers: list[str],
    grid_mm: float,
    allow_vias: bool,
    note: str = "repair",
) -> None:
    """MST re-route one net into trial, painting obstacles as we go."""
    pins = board.nets.get(net) or []
    anchors: list[tuple[float, float]] = []
    for ref, pad in pins:
        if ref in board.components:
            anchors.append(fanout_anchor(board, ref, net, pad_num=str(pad)))
    uniq: list[tuple[float, float]] = []
    for a in anchors:
        if not any(_dist(a, u) < 0.05 for u in uniq):
            uniq.append(a)
    anchors = uniq
    if len(anchors) < 2:
        return

    width = _net_width(config, net)
    net_layers = _cpx_layer_order(net, layers)
    remaining = set(range(1, len(anchors)))
    tree = {0}
    net_len = 0.0
    net_segs = 0
    net_vias = 0
    layer_set: set[str] = set()
    methods: list[str] = []
    # Slightly finer grid for repair to squeeze past dense copper
    g = min(grid_mm, 0.2) if grid_mm > 0.15 else grid_mm
    while remaining:
        best_e: tuple[float, int, int] | None = None
        for i in tree:
            for j in remaining:
                d = _dist(anchors[i], anchors[j])
                if best_e is None or d < best_e[0]:
                    best_e = (d, i, j)
        assert best_e is not None
        _, ia, ib = best_e
        remaining.remove(ib)
        tree.add(ib)
        meth: list[str] = []
        path, new_vias = _route_point_to_point(
            anchors[ia],
            anchors[ib],
            net,
            om,
            layers=net_layers,
            grid_mm=g,
            allow_vias=allow_vias,
            width_mm=width,
            method_out=meth,
            k_homotopy=1,
        )
        if path is None:
            methods.append("unrouted_edge")
            continue
        methods.extend(meth)
        for i in range(len(path) - 1):
            x1, y1, ly1 = path[i]
            x2, y2, ly2 = path[i + 1]
            if ly1 != ly2:
                continue
            trial.segments.append(
                RouteSegment(
                    x1=x1, y1=y1, x2=x2, y2=y2, layer=ly1, net=net, width_mm=width
                )
            )
            d = _dist((x1, y1), (x2, y2))
            trial.total_length_mm += d
            net_len += d
            net_segs += 1
            layer_set.add(ly1)
            om.paint_trace(x1, y1, x2, y2, ly1, width, net)
        for v in new_vias:
            trial.vias.append(v)
            trial.via_count += 1
            net_vias += 1
            for ly in layers:
                om.add_rect(v.x, v.y, v.size_mm, v.size_mm, ly, net=net, inflate=True)

    open_e = methods.count("unrouted_edge")
    status = "ok"
    if open_e and net_segs == 0:
        status = "unrouted"
        if net not in trial.unrouted_nets:
            trial.unrouted_nets.append(net)
    elif open_e:
        status = "partial"
    trial.net_reports.append(
        NetRouteReport(
            net=net,
            pins=len(pins),
            length_mm=net_len,
            segments=net_segs,
            vias=net_vias,
            layers=sorted(layer_set),
            status=status,
            method="+".join(dict.fromkeys(methods)) if methods else note,
            notes=[note],
        )
    )


def repair_drc_conflicts(
    result: RouteResult,
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    clearance_mm: float = 0.2,
    grid_mm: float = 0.5,
    layers: list[str] | None = None,
    allow_vias: bool = True,
    max_rounds: int = 5,
) -> RouteResult:
    """Rip-up & re-route nets that participate in shorts / hard spacing hits.

    Strategy:
    1. Multi-net batch rip of the worst conflict cluster, re-route high-priority first
    2. Single-net rip for residual hits
    Keeps better-of (lower DRC count) so score can only improve.
    """
    layers = layers or list(board.copper_layers) or ["F.Cu", "B.Cu"]
    if not result.segments:
        return result

    def _viol_count(r: RouteResult) -> int:
        return int(native_drc_check(r, clearance_mm=clearance_mm, board=board)["violations"])

    def _strip_nets(base: RouteResult, nets: set[str]) -> RouteResult:
        segs = [s for s in base.segments if s.net not in nets]
        vias = [v for v in base.vias if v.net not in nets]
        trial = RouteResult(
            segments=list(segs),
            vias=list(vias),
            via_count=len(vias),
            total_length_mm=sum(_dist((s.x1, s.y1), (s.x2, s.y2)) for s in segs),
            unrouted_nets=[u for u in base.unrouted_nets if u not in nets],
            clearance_violations=base.clearance_violations,
            notes=list(base.notes),
            net_reports=[r for r in base.net_reports if r.net not in nets],
            quality=dict(base.quality or {}),
        )
        return trial

    def _paint_om(trial: RouteResult) -> ObstacleMap:
        om = build_obstacle_map(board, clearance_mm=clearance_mm, layers=layers)
        for s in trial.segments:
            om.paint_trace(s.x1, s.y1, s.x2, s.y2, s.layer, s.width_mm, s.net)
        for v in trial.vias:
            for ly in layers:
                om.add_rect(v.x, v.y, v.size_mm, v.size_mm, ly, net=v.net, inflate=True)
        return om

    best = result
    best_v = _viol_count(best)
    if best_v == 0:
        return best

    rounds = 0
    while rounds < max_rounds and best_v > 0:
        rounds += 1
        rep = native_drc_check(best, clearance_mm=clearance_mm, board=board)
        net_hits: dict[str, int] = {}
        for item in rep.get("items") or []:
            if item.get("kind") not in ("short", "spacing"):
                continue
            w = 3 if item.get("kind") == "short" else 1
            for key in ("net_a", "net_b"):
                n = item.get(key)
                if n and n not in ("Edge.Cuts", "?"):
                    net_hits[n] = net_hits.get(n, 0) + w
        if not net_hits:
            break

        def _rip_key(n: str) -> tuple:
            # Most hits first, then lowest priority (signals before power), fewer pins last
            return (-net_hits[n], _net_priority(config, n), -len(board.nets.get(n, [])), n)

        candidates = sorted(net_hits.keys(), key=_rip_key)
        improved = False

        # --- Batch: rip a conflict cluster and re-route high-priority first ---
        batch = candidates[: max(3, min(8, len(candidates)))]
        if len(batch) >= 2:
            trial = _strip_nets(best, set(batch))
            om = _paint_om(trial)
            # Re-route high priority first (opposite of rip key's priority term)
            order = sorted(batch, key=lambda n: (-_net_priority(config, n), len(board.nets.get(n, [])), n))
            for net in order:
                _reroute_net_into(
                    trial,
                    net,
                    board,
                    config,
                    om=om,
                    layers=layers,
                    grid_mm=grid_mm,
                    allow_vias=allow_vias,
                    note=f"drc_repair_batch r{rounds}",
                )
            v_new = _viol_count(trial)
            better = v_new < best_v or (
                v_new == best_v and len(trial.unrouted_nets) < len(best.unrouted_nets)
            )
            if better:
                best = trial
                best_v = v_new
                best.notes.append(
                    f"drc_repair: batch {','.join(batch)} → violations {best_v} (round {rounds})"
                )
                improved = True
                if best_v == 0:
                    break

        # --- Single-net residual rip ---
        if best_v > 0:
            rep = native_drc_check(best, clearance_mm=clearance_mm, board=board)
            net_hits = {}
            for item in rep.get("items") or []:
                if item.get("kind") not in ("short", "spacing"):
                    continue
                w = 3 if item.get("kind") == "short" else 1
                for key in ("net_a", "net_b"):
                    n = item.get(key)
                    if n and n not in ("Edge.Cuts", "?"):
                        net_hits[n] = net_hits.get(n, 0) + w
            singles = sorted(net_hits.keys(), key=_rip_key)[:4] if net_hits else []
            for net in singles:
                trial = _strip_nets(best, {net})
                om = _paint_om(trial)
                _reroute_net_into(
                    trial,
                    net,
                    board,
                    config,
                    om=om,
                    layers=layers,
                    grid_mm=grid_mm,
                    allow_vias=allow_vias,
                    note=f"drc_repair r{rounds}",
                )
                v_new = _viol_count(trial)
                better = v_new < best_v or (
                    v_new == best_v and len(trial.unrouted_nets) < len(best.unrouted_nets)
                )
                if better:
                    best = trial
                    best_v = v_new
                    best.notes.append(
                        f"drc_repair: re-routed {net} → violations {best_v} (round {rounds})"
                    )
                    improved = True
                    if best_v == 0:
                        break

        if not improved:
            break

    if rounds:
        best.notes.append(f"drc_repair: {rounds} round(s), final violations={best_v}")
    return best


def purge_shorting_copper(
    result: RouteResult,
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    clearance_mm: float = 0.2,
    max_passes: int = 80,
) -> RouteResult:
    """Drop minimal copper that hard-shorts or escapes Edge.Cuts (open > illegal).

    Per pass removes only the single closest segment of the lower-priority net
    (or the outside segment for outline hits) so we do not nuke whole nets.
    """
    if not result.segments:
        return result

    trial = RouteResult(
        segments=list(result.segments),
        vias=list(result.vias),
        via_count=result.via_count,
        total_length_mm=result.total_length_mm,
        unrouted_nets=list(result.unrouted_nets),
        clearance_violations=result.clearance_violations,
        notes=list(result.notes),
        net_reports=list(result.net_reports),
        quality=dict(result.quality or {}),
    )
    removed = 0

    def _mark_victim(victim: str) -> None:
        segs_left = sum(1 for s in trial.segments if s.net == victim)
        for nr in trial.net_reports:
            if nr.net != victim:
                continue
            nr.segments = segs_left
            if segs_left == 0:
                nr.status = "unrouted"
                if victim not in trial.unrouted_nets:
                    trial.unrouted_nets.append(victim)
            else:
                nr.status = "partial"
                note = "purged illegal copper"
                if note not in (nr.notes or []):
                    nr.notes = list(nr.notes or []) + [note]
            break

    def _drop_index(i: int) -> bool:
        nonlocal removed
        if i < 0 or i >= len(trial.segments):
            return False
        victim = trial.segments[i].net
        trial.segments.pop(i)
        removed += 1
        _rebuild_totals(trial)
        _mark_victim(victim)
        return True

    for _ in range(max_passes):
        rep = native_drc_check(trial, clearance_mm=clearance_mm, board=board)
        items = rep.get("items") or []
        # Prefer shorts first, then outline; leave pure spacing for repair
        shorts = [it for it in items if it.get("kind") == "short"]
        outlines = [it for it in items if it.get("kind") == "outline"]
        it = shorts[0] if shorts else (outlines[0] if outlines else None)
        if it is None:
            break

        if it.get("kind") == "outline":
            victim = it.get("net_a") or ""
            px, py = float(it.get("x") or 0), float(it.get("y") or 0)
            layer = it.get("layer") or ""
            best_i, best_d = -1, 1e9
            for i, s in enumerate(trial.segments):
                if victim and s.net != victim:
                    continue
                if layer and layer != "via" and s.layer != layer:
                    continue
                d = min(
                    _dist((px, py), (s.x1, s.y1)),
                    _dist((px, py), (s.x2, s.y2)),
                    _point_seg_dist(px, py, s.x1, s.y1, s.x2, s.y2),
                )
                if d < best_d:
                    best_d, best_i = d, i
            if best_i < 0 or not _drop_index(best_i):
                break
            continue

        na, nb = it.get("net_a") or "", it.get("net_b") or ""
        if not na or not nb or na in ("Edge.Cuts", "?") or nb in ("Edge.Cuts", "?"):
            break
        pa, pb = _net_priority(config, na), _net_priority(config, nb)
        # Lower priority loses; equal priority → fewer pins (easier to re-open)
        if pa < pb:
            victim = na
        elif pb < pa:
            victim = nb
        else:
            victim = na if len(board.nets.get(na, [])) <= len(board.nets.get(nb, [])) else nb
        layer = it.get("layer") or ""
        px, py = float(it.get("x") or 0), float(it.get("y") or 0)

        best_i, best_d = -1, 1e9
        for i, s in enumerate(trial.segments):
            if s.net != victim:
                continue
            if layer and layer != "via" and s.layer != layer:
                continue
            d = min(
                _dist((px, py), (s.x1, s.y1)),
                _dist((px, py), (s.x2, s.y2)),
                _point_seg_dist(px, py, s.x1, s.y1, s.x2, s.y2),
            )
            if d < best_d:
                best_d, best_i = d, i
        if best_i < 0:
            # Via-only short — drop one victim via nearest the hit
            best_vi, best_vd = -1, 1e9
            for i, v in enumerate(trial.vias):
                if v.net != victim:
                    continue
                d = _dist((px, py), (v.x, v.y))
                if d < best_vd:
                    best_vd, best_vi = d, i
            if best_vi < 0:
                break
            trial.vias.pop(best_vi)
            trial.via_count = len(trial.vias)
            removed += 1
            _mark_victim(victim)
            continue
        if not _drop_index(best_i):
            break

    if removed:
        trial.notes.append(f"purge_illegal: removed {removed} segment/via piece(s)")
        live_nets = {s.net for s in trial.segments}
        keep_v = [v for v in trial.vias if v.net in live_nets]
        if len(keep_v) != len(trial.vias):
            trial.notes.append(
                f"purge_illegal: dropped {len(trial.vias) - len(keep_v)} orphan via(s)"
            )
            trial.vias = keep_v
            trial.via_count = len(keep_v)
        _rebuild_totals(trial)
    return trial


def native_drc_check(
    result: RouteResult,
    *,
    clearance_mm: float = 0.2,
    max_violations: int = 200,
    board: BoardModel | None = None,
) -> dict[str, Any]:
    """Exact copper DRC in the C++ core: foreign-net shorts, spacing, vias.

    Optionally counts Edge.Cuts outline escapes when ``board`` has outline
    graphics. Returns {violations, shorts, spacing, outline_outside, items:...}.
    """
    n = _native_core()
    net_ids: dict[str, int] = {}
    layer_ids: dict[str, int] = {}

    def _nid(name: str) -> int:
        return net_ids.setdefault(name, len(net_ids))

    def _lid(name: str) -> int:
        return layer_ids.setdefault(name, len(layer_ids))

    segs = [
        (s.x1, s.y1, s.x2, s.y2, s.width_mm, _lid(s.layer), _nid(s.net))
        for s in result.segments
    ]
    vias = [(v.x, v.y, v.size_mm, _nid(v.net)) for v in result.vias]
    raw = n.drc_check(
        segs, vias, clearance_mm=float(clearance_mm), max_violations=int(max_violations)
    )
    id2net = {v: k for k, v in net_ids.items()}
    id2layer = {v: k for k, v in layer_ids.items()}
    items: list[dict[str, Any]] = [
        {
            "kind": str(v["kind"]),
            "net_a": id2net.get(v["net_a"], "?"),
            "net_b": id2net.get(v["net_b"], "?"),
            "layer": id2layer.get(v["layer"], "via") if int(v["layer"]) >= 0 else "via",
            "x": round(float(v["x"]), 3),
            "y": round(float(v["y"]), 3),
            "dist_mm": round(float(v["dist"]), 4),
            "need_mm": round(float(v["need"]), 4),
        }
        for v in raw
    ]
    shorts = sum(1 for v in items if v["kind"] == "short")
    spacing = sum(1 for v in items if v["kind"] == "spacing")
    outline_outside = 0
    if board is not None:
        poly = outline_polygon_from_board(board)
        if poly and len(poly) >= 3:
            seen: set[tuple[float, float]] = set()
            for s in result.segments:
                for pt in (
                    (s.x1, s.y1),
                    (s.x2, s.y2),
                    (0.5 * (s.x1 + s.x2), 0.5 * (s.y1 + s.y2)),
                ):
                    key = (round(pt[0], 3), round(pt[1], 3))
                    if key in seen:
                        continue
                    seen.add(key)
                    if not point_in_polygon(pt[0], pt[1], poly):
                        outline_outside += 1
                        if len(items) < max_violations:
                            items.append(
                                {
                                    "kind": "outline",
                                    "net_a": s.net,
                                    "net_b": "Edge.Cuts",
                                    "layer": s.layer,
                                    "x": round(pt[0], 3),
                                    "y": round(pt[1], 3),
                                    "dist_mm": 0.0,
                                    "need_mm": 0.0,
                                }
                            )
            for v in result.vias:
                if not point_in_polygon(v.x, v.y, poly):
                    outline_outside += 1
                    if len(items) < max_violations:
                        items.append(
                            {
                                "kind": "outline",
                                "net_a": v.net,
                                "net_b": "Edge.Cuts",
                                "layer": "via",
                                "x": round(v.x, 3),
                                "y": round(v.y, 3),
                                "dist_mm": 0.0,
                                "need_mm": 0.0,
                            }
                        )
    return {
        "violations": len(items),
        "shorts": shorts,
        "spacing": spacing,
        "outline_outside": outline_outside,
        "items": items,
    }


def attach_router_drc(
    result: RouteResult,
    *,
    clearance_mm: float = 0.2,
    board: BoardModel | None = None,
) -> dict[str, Any]:
    """Run the built-in DRC and record it on the result (authoritative count).

    Always-on: shorts, spacing, via clearance (native), plus optional Edge.Cuts
    outline escapes when ``board`` is provided. Sets ``clearance_violations``
    so grades and UI reflect legality.
    """
    rep = native_drc_check(result, clearance_mm=clearance_mm, board=board)
    result.clearance_violations = int(rep["violations"])
    samples = []
    for v in rep["items"][:10]:
        if v["kind"] == "outline":
            samples.append(
                f"{v['layer']}: {v['net_a']} outside Edge.Cuts @({v['x']},{v['y']})"
            )
        else:
            samples.append(
                f"{v['layer']}: {v['net_a']}×{v['net_b']} {v['kind']} "
                f"d={v['dist_mm']}<{v['need_mm']} @({v['x']},{v['y']})"
            )
    result.quality = {
        **(result.quality or {}),
        "drc": {
            "violations": rep["violations"],
            "shorts": rep["shorts"],
            "spacing": rep["spacing"],
            "outline_outside": rep.get("outline_outside", 0),
            "samples": samples,
        },
    }
    oo = int(rep.get("outline_outside") or 0)
    result.notes.append(
        f"router_drc: {rep['violations']} violation(s) "
        f"({rep['shorts']} short, {rep['spacing']} spacing"
        f"{f', {oo} outside outline' if oo else ''}) "
        f"@ clearance {clearance_mm}mm"
    )
    return rep


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

    def _path_legal(pts: list[tuple[float, float]], layer: str) -> bool:
        if len(pts) < 2:
            return False
        for i in range(len(pts) - 1):
            if om.segment_blocked(
                pts[i][0],
                pts[i][1],
                pts[i + 1][0],
                pts[i + 1][1],
                layer,
                net,
                width_mm=width_mm,
            ):
                return False
        return True

    # Prefer K-homotopy same-layer paths when k_homotopy > 1 (must still pass DRC paint)
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
                # Prefer legal candidates only (homotopy can return LOS that later nets collide)
                legal = [c for c in cands if _path_legal(list(c.points), layer)]
                best = pick_best_homotopy(legal) if legal else None
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
        # Primary (first preferred) with every other layer — better CPX escape
        for j in range(1, len(layers)):
            pairs.append((layers[0], layers[j]))
            pairs.append((layers[j], layers[0]))
        pairs.append((layers[-1], layers[0]))
        if len(layers) > 2:
            pairs.append((layers[1], layers[2] if len(layers) > 2 else layers[-1]))

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


def strip_physics_router_copper(text: str) -> str:
    """Remove legacy physicsRouter marker blocks if present."""
    for begin_tok, end_tok in (
        ("(physics_router_copper begin)", "physics_router_copper end"),
        ("generator_add physics_router_topor)", "physics_router_topor_end"),
    ):
        begin = text.find(begin_tok)
        if begin < 0:
            continue
        begin = text.rfind("\n", 0, begin) + 1
        end = text.find(end_tok, begin)
        if end < 0:
            continue
        line_end = text.find("\n", end)
        if line_end < 0:
            line_end = len(text)
        text = text[:begin] + text[line_end + 1 :]
    return text


def strip_board_tracks_and_vias(text: str) -> str:
    """Remove board-level ``(segment …)`` / ``(via …)`` lines.

    Footprints, nets, zones, and Edge.Cuts stay. Used so KiCad DRC / re-apply
    sees only autorouter copper (not stacked on prior tracks).
    """
    import re

    text = re.sub(r"^[ \t]*\(segment\b.*\)\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*\(via\b.*\)\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def parse_kicad_net_map(pcb_text: str) -> dict[str, int]:
    """Map net name → KiCad net code from ``(net N "NAME")`` declarations.

    Without correct codes, exported copper is written as net 0 and KiCad DRC
    cannot see foreign-net shorts (everything looks like the same/no net).
    """
    import re

    out: dict[str, int] = {}
    # Top-level board net table entries: (net 3 "CPX-0")
    for m in re.finditer(r'\(net\s+(\d+)\s+"([^"]*)"\)', pcb_text):
        code = int(m.group(1))
        name = m.group(2)
        if name == "":
            continue
        # Prefer first (board table) occurrence; pad-level repeats same code
        if name not in out:
            out[name] = code
    return out


def _tstamp_token(seed: int) -> str:
    hex32 = f"{abs(seed) % (16**32):032x}"
    return f"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"


def append_routes_to_kicad_pcb(
    source_path: str,
    dest_path: str,
    result: RouteResult,
    *,
    replace_previous: bool = True,
    clear_existing_copper: bool = False,
) -> Path:
    """Append segment and via S-expressions before the final closing paren.

    When ``replace_previous`` is True, clears board tracks/vias and legacy
    physicsRouter blocks so re-apply does not stack copper.

    Format matches HALO/pcbnew dialect (kicad-cli loadable)::

        (segment (start x y) (end x y) (width w) (layer "F.Cu") (net N) (tstamp …))
    """

    src = Path(source_path)
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8", errors="replace")
    text = strip_physics_router_copper(text)
    if replace_previous or clear_existing_copper:
        text = strip_board_tracks_and_vias(text)
    net_map = parse_kicad_net_map(text)
    stripped = text.rstrip()
    if not stripped.endswith(")"):
        raise ValueError("Invalid kicad_pcb: expected trailing ')'")
    body = stripped[:-1]
    chunks = ["\n"]
    for s in result.segments:
        code = int(net_map.get(s.net, 0))
        ts = _tstamp_token(hash((s.x1, s.y1, s.x2, s.y2, s.net, s.layer)))
        chunks.append(
            f'  (segment (start {s.x1:.6f} {s.y1:.6f}) (end {s.x2:.6f} {s.y2:.6f}) '
            f'(width {s.width_mm:.4f}) (layer "{s.layer}") (net {code}) '
            f"(tstamp {ts}))\n"
        )
    for v in result.vias:
        code = int(net_map.get(v.net, 0))
        ts = _tstamp_token(hash((v.x, v.y, v.net, v.size_mm)))
        la, lb = v.layers[0], v.layers[1]
        chunks.append(
            f'  (via (at {v.x:.6f} {v.y:.6f}) (size {v.size_mm:.4f}) '
            f'(drill {v.drill_mm:.4f}) (layers "{la}" "{lb}") '
            f"(net {code}) (tstamp {ts}))\n"
        )
    dest.write_text(body + "".join(chunks) + ")\n", encoding="utf-8")
    return dest
