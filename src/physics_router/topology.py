"""TopoR-style topology-first infrastructure.

Three simultaneous representations (see docs/ARCHITECTURE_ROUTER.md):

1. **Topological** — homotopy / obstacle-passing class (survives small moves)
2. **Sparse geometric graph** — radar-scan / portal candidates (LineExplore-like)
3. **Exact geometry** — segments/arcs after topology is chosen

Also: negotiated congestion (route with soft overuse, raise historical cost),
topology signatures for multi-homotopy alternatives, multiobjective score vectors.

References: Dayan 1997 rubberband; US 7,937,681 topology→geometry feedback;
3D LineExplore (Sci. Reports 2026); negotiated congestion (FPGA literature).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from physics_router.models import BoardModel
from physics_router.router import ObstacleMap, RouteResult, RouteSegment, _dist


# ---------------------------------------------------------------------------
# Topology signature (homotopy class)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopologySignature:
    """Compact encoding of which side of obstacles a path took.

    Two paths with the same signature are the same topological class even if
    coordinates differ (elastic / rubberband geometry).
    """

    net: str
    layer: str
    # Ordered obstacle ids (hashed centers) with left/right of directed path
    obstacle_sides: tuple[str, ...] = ()
    via_count: int = 0
    layers_used: tuple[str, ...] = ()

    def key(self) -> str:
        return (
            f"{self.net}|{self.layer}|v{self.via_count}|"
            f"{','.join(self.layers_used)}|{','.join(self.obstacle_sides)}"
        )


def _side_of_segment(
    ax: float, ay: float, bx: float, by: float, px: float, py: float
) -> str:
    """Return L/R/on for point relative to directed segment A→B."""
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) < 1e-9:
        return "on"
    return "L" if cross > 0 else "R"


def signature_for_polyline(
    net: str,
    layer: str,
    points: list[tuple[float, float]],
    om: ObstacleMap,
    *,
    via_count: int = 0,
    layers_used: Iterable[str] | None = None,
) -> TopologySignature:
    """Build a topology signature from a free-angle polyline vs obstacle map."""
    if len(points) < 2:
        return TopologySignature(net=net, layer=layer, via_count=via_count)

    sides: list[str] = []
    seen: set[str] = set()
    # Use first→last as primary directed axis for side tests
    ax, ay = points[0]
    bx, by = points[-1]
    for ob in om.obstacles.get(layer, []):
        if ob.net == net:
            continue
        # Only obstacles near the path corridor
        mid = ((ax + bx) / 2, (ay + by) / 2)
        if _dist((ob.cx, ob.cy), mid) > max(_dist((ax, ay), (bx, by)), 1.0) * 0.85 + 8.0:
            continue
        side = _side_of_segment(ax, ay, bx, by, ob.cx, ob.cy)
        if side == "on":
            continue
        oid = f"{side}@{ob.cx:.1f},{ob.cy:.1f}"
        if oid not in seen:
            seen.add(oid)
            sides.append(oid)
    sides.sort()
    ly = tuple(sorted(set(layers_used or [layer])))
    return TopologySignature(
        net=net,
        layer=layer,
        obstacle_sides=tuple(sides[:24]),
        via_count=via_count,
        layers_used=ly,
    )


def signatures_from_route(result: RouteResult, om: ObstacleMap) -> list[dict[str, Any]]:
    """Extract per-net topology signatures from a completed route."""
    by_net: dict[str, list[RouteSegment]] = {}
    for s in result.segments:
        by_net.setdefault(s.net, []).append(s)
    out: list[dict[str, Any]] = []
    via_by_net: dict[str, int] = {}
    for v in result.vias:
        via_by_net[v.net] = via_by_net.get(v.net, 0) + 1
    for net, segs in by_net.items():
        # Build a rough polyline from first segment chain
        pts: list[tuple[float, float]] = [(segs[0].x1, segs[0].y1)]
        for s in segs:
            pts.append((s.x2, s.y2))
        layers = sorted({s.layer for s in segs})
        sig = signature_for_polyline(
            net,
            layers[0] if layers else "F.Cu",
            pts,
            om,
            via_count=via_by_net.get(net, 0),
            layers_used=layers,
        )
        out.append({"net": net, "signature": sig.key(), "layers": layers})
    return out


# ---------------------------------------------------------------------------
# Sparse geometric candidates (LineExplore-like radar scan)
# ---------------------------------------------------------------------------


def radar_scan_points(
    origin: tuple[float, float],
    goal: tuple[float, float],
    om: ObstacleMap,
    layer: str,
    net: str,
    *,
    rays: int = 16,
    max_range_mm: float | None = None,
    grid_mm: float = 0.5,
) -> list[tuple[float, float]]:
    """Generate geometrically meaningful free-space waypoints.

    Cast rays from the origin; keep first free sample near obstacle boundaries
    and along the goal direction (continuous-space exploration idea from
    3D LineExplore — Sci. Reports 2026).
    """
    ox, oy = origin
    gx, gy = goal
    span = _dist(origin, goal)
    rmax = max_range_mm if max_range_mm is not None else max(span * 1.4, 12.0)
    pts: list[tuple[float, float]] = []

    # Goal-directed samples
    for t in (0.25, 0.5, 0.75):
        pts.append((ox + (gx - ox) * t, oy + (gy - oy) * t))

    for i in range(rays):
        ang = 2.0 * math.pi * i / rays
        dx, dy = math.cos(ang), math.sin(ang)
        # Walk outward; record transition free→blocked midpoints
        prev_free = True
        step = max(grid_mm, 0.35)
        dist = step
        while dist <= rmax:
            x, y = ox + dx * dist, oy + dy * dist
            if not om.in_bounds(x, y):
                break
            free = not om.blocked(x, y, layer, net)
            if prev_free and not free:
                # back up one step — free-space boundary point
                bx, by = ox + dx * (dist - step), oy + dy * (dist - step)
                if om.in_bounds(bx, by) and not om.blocked(bx, by, layer, net):
                    pts.append((bx, by))
                break
            if free and dist > span * 0.3:
                # sparse free sample
                if int(dist / step) % 4 == 0:
                    pts.append((x, y))
            prev_free = free
            dist += step

    # Obstacle corner portals (visibility graph seeds)
    reach = rmax
    for ob in om.obstacles.get(layer, []):
        if ob.net == net:
            continue
        if _dist((ob.cx, ob.cy), origin) > reach and _dist((ob.cx, ob.cy), goal) > reach:
            continue
        for c in ob.corners(pad=grid_mm * 1.2):
            if om.in_bounds(c[0], c[1]) and not om.blocked(c[0], c[1], layer, net):
                pts.append(c)

    # Dedup
    uniq: list[tuple[float, float]] = []
    for p in pts:
        pr = (round(p[0], 3), round(p[1], 3))
        if not any(_dist(pr, u) < grid_mm * 0.4 for u in uniq):
            uniq.append(pr)
    return uniq[:48]


# ---------------------------------------------------------------------------
# Negotiated congestion map
# ---------------------------------------------------------------------------


@dataclass
class CongestionMap:
    """Historical + present congestion costs on a coarse cell grid.

    Negotiated-congestion philosophy (FPGA literature; TopoR "route first,
    resolve later"): allow temporary overuse, raise cost of persistently
    congested cells across iterations so nets spread into alternate homotopy.
    """

    cell_mm: float = 1.0
    present: dict[tuple[int, int, str], float] = field(default_factory=dict)
    historical: dict[tuple[int, int, str], float] = field(default_factory=dict)
    present_weight: float = 1.0
    historical_weight: float = 0.35
    historical_decay: float = 0.92
    historical_boost: float = 0.45

    def _key(self, x: float, y: float, layer: str) -> tuple[int, int, str]:
        c = self.cell_mm
        return (int(math.floor(x / c)), int(math.floor(y / c)), layer)

    def cost(self, x: float, y: float, layer: str) -> float:
        k = self._key(x, y, layer)
        return (
            self.present_weight * self.present.get(k, 0.0)
            + self.historical_weight * self.historical.get(k, 0.0)
        )

    def paint_segment(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: str,
        *,
        amount: float = 1.0,
    ) -> None:
        length = max(_dist((x1, y1), (x2, y2)), 0.01)
        steps = max(1, int(math.ceil(length / (self.cell_mm * 0.5))))
        for i in range(steps + 1):
            t = i / steps
            x = x1 + (x2 - x1) * t
            y = y1 + (y2 - y1) * t
            k = self._key(x, y, layer)
            self.present[k] = self.present.get(k, 0.0) + amount

    def paint_route(self, result: RouteResult, *, amount: float = 1.0) -> None:
        for s in result.segments:
            self.paint_segment(s.x1, s.y1, s.x2, s.y2, s.layer, amount=amount)

    def clear_present(self) -> None:
        self.present.clear()

    def negotiate(self) -> None:
        """End-of-iteration: boost historical cost where present congestion is high."""
        # Decay all historical
        for k in list(self.historical.keys()):
            self.historical[k] *= self.historical_decay
            if self.historical[k] < 0.05:
                del self.historical[k]
        # Boost cells that were used (conflict proxies: multi-use or any use)
        for k, v in self.present.items():
            if v >= 1.0:
                self.historical[k] = self.historical.get(k, 0.0) + self.historical_boost * v
        self.clear_present()

    def edge_cost(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: str,
        *,
        length_weight: float = 1.0,
        via_penalty: float = 0.0,
    ) -> float:
        length = _dist((x1, y1), (x2, y2))
        # Sample congestion along edge
        cong = 0.0
        samples = max(1, int(length / max(self.cell_mm, 0.2)))
        for i in range(samples + 1):
            t = i / samples
            cong += self.cost(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, layer)
        cong /= samples + 1
        return length_weight * length + cong + via_penalty


# ---------------------------------------------------------------------------
# Multiobjective score vector (Pareto-friendly)
# ---------------------------------------------------------------------------


@dataclass
class ScoreVector:
    """Do not collapse objectives too early — designer may prefer different tradeoffs."""

    unrouted: int = 0
    hard_violations: int = 0
    via_count: int = 0
    total_length_mm: float = 0.0
    max_congestion: float = 0.0
    soft_violations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "unrouted": self.unrouted,
            "hard_violations": self.hard_violations,
            "via_count": self.via_count,
            "total_length_mm": round(self.total_length_mm, 3),
            "max_congestion": round(self.max_congestion, 3),
            "soft_violations": self.soft_violations,
        }

    def dominates(self, other: ScoreVector) -> bool:
        """True if self is Pareto-better or equal on all, strict on one (minimize all)."""
        a = (
            self.unrouted,
            self.hard_violations,
            self.via_count,
            self.total_length_mm,
            self.max_congestion,
            self.soft_violations,
        )
        b = (
            other.unrouted,
            other.hard_violations,
            other.via_count,
            other.total_length_mm,
            other.max_congestion,
            other.soft_violations,
        )
        le = all(x <= y for x, y in zip(a, b))
        lt = any(x < y for x, y in zip(a, b))
        return le and lt


def score_vector_from_route(
    result: RouteResult, congestion: CongestionMap | None = None
) -> ScoreVector:
    max_c = 0.0
    if congestion is not None:
        max_c = max(congestion.present.values(), default=0.0)
        max_c = max(max_c, max(congestion.historical.values(), default=0.0))
    return ScoreVector(
        unrouted=len(result.unrouted_nets),
        hard_violations=int(result.clearance_violations),
        via_count=result.via_count,
        total_length_mm=result.total_length_mm,
        max_congestion=max_c,
        soft_violations=sum(
            1 for r in result.net_reports if r.status == "soft_violation"
        ),
    )


def pareto_front(
    named: list[tuple[str, RouteResult, ScoreVector]],
) -> list[tuple[str, RouteResult, ScoreVector]]:
    """Keep non-dominated complete-board variants."""
    front: list[tuple[str, RouteResult, ScoreVector]] = []
    for cand in named:
        dominated = False
        for other in named:
            if other[0] == cand[0]:
                continue
            if other[2].dominates(cand[2]):
                dominated = True
                break
        if not dominated:
            front.append(cand)
    return front or list(named)


# ---------------------------------------------------------------------------
# Board-level topology index (for incremental invalidation later)
# ---------------------------------------------------------------------------


def board_topology_index(board: BoardModel) -> dict[str, Any]:
    """Board hypergraph, crossing conflicts, layer colors, and physical extent."""
    from physics_router.graph_theory import plan_graph_topology

    comps = list(board.components.values())
    graph = plan_graph_topology(board)
    return {
        "components": len(comps),
        "nets": len(board.nets),
        "copper_layers": list(board.copper_layers or ["F.Cu", "B.Cu"]),
        "extent": {
            "w": board.width_mm,
            "h": board.height_mm,
        },
        "graph": graph.to_dict(),
    }
