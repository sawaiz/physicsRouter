"""Post-connect free-angle re-geometry (TopoR-style).

After connectivity exists (MST / free-angle / vias), reshape copper without
changing topology:

1. **Subdivide** long segments → multi-bend degrees of freedom
2. **Spacing field** — repel intermediate vertices from foreign copper / keepouts
3. **Smooth / arc-approximate** sharp corners (chord samples, free-angle look)
4. **Metrics** — bends, min spacing, arc segments, length (TopoR scorecard)

Inspired by Eremex TopoR competitive advantages: topology first, then continuous
re-geometry for equal spacing and efficient surface use (not H/V templates).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from physics_router.models import BoardModel
from physics_router.router import (
    ObstacleMap,
    RouteResult,
    RouteSegment,
    build_obstacle_map,
    _dist,
    _point_seg_dist,
)


def count_bends(pts: list[tuple[float, float]], *, min_cross: float = 1e-3) -> int:
    """Non-collinear triples along a polyline."""
    if len(pts) < 3:
        return 0
    n = 0
    for i in range(1, len(pts) - 1):
        ax = pts[i][0] - pts[i - 1][0]
        ay = pts[i][1] - pts[i - 1][1]
        bx = pts[i + 1][0] - pts[i][0]
        by = pts[i + 1][1] - pts[i][1]
        if abs(ax * by - ay * bx) > min_cross:
            n += 1
    return n


def polyline_from_segments(segs: list[RouteSegment]) -> list[tuple[float, float]]:
    if not segs:
        return []
    pts = [(segs[0].x1, segs[0].y1)]
    for s in segs:
        last = pts[-1]
        if _dist(last, (s.x1, s.y1)) < 0.08:
            pts.append((s.x2, s.y2))
        elif _dist(last, (s.x2, s.y2)) < 0.08:
            pts.append((s.x1, s.y1))
        else:
            pts.append((s.x1, s.y1))
            pts.append((s.x2, s.y2))
    out = [pts[0]]
    for p in pts[1:]:
        if _dist(out[-1], p) > 1e-6:
            out.append(p)
    return out


def segments_from_polyline(
    pts: list[tuple[float, float]], layer: str, net: str, width_mm: float
) -> list[RouteSegment]:
    segs: list[RouteSegment] = []
    for i in range(len(pts) - 1):
        if _dist(pts[i], pts[i + 1]) < 1e-9:
            continue
        segs.append(
            RouteSegment(
                x1=pts[i][0],
                y1=pts[i][1],
                x2=pts[i + 1][0],
                y2=pts[i + 1][1],
                layer=layer,
                net=net,
                width_mm=width_mm,
            )
        )
    return segs


def subdivide_polyline(
    pts: list[tuple[float, float]],
    *,
    max_seg_mm: float = 2.5,
    min_points: int = 0,
) -> list[tuple[float, float]]:
    """Insert midpoints on long edges so spacing forces can introduce multi-bends."""
    if len(pts) < 2:
        return pts
    out: list[tuple[float, float]] = [pts[0]]
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        d = _dist(a, b)
        n = max(1, int(math.ceil(d / max(max_seg_mm, 0.5))))
        for k in range(1, n):
            t = k / n
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        out.append(b)
    # Optional pad to min_points by splitting longest
    while min_points > 0 and len(out) < min_points:
        best_i, best_d = 0, 0.0
        for i in range(len(out) - 1):
            dd = _dist(out[i], out[i + 1])
            if dd > best_d:
                best_d, best_i = dd, i
        if best_d < 0.3:
            break
        a, b = out[best_i], out[best_i + 1]
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        out.insert(best_i + 1, mid)
    return out


def spacing_repel_polyline(
    pts: list[tuple[float, float]],
    layer: str,
    net: str,
    om: ObstacleMap,
    *,
    width_mm: float = 0.25,
    clearance_mm: float = 0.2,
    iterations: int = 20,
    step: float = 0.12,
    target_extra_mm: float = 0.15,
) -> list[tuple[float, float]]:
    """Push intermediate vertices away from foreign copper to equalize spacing.

    Endpoints fixed (pins/vias). Soft repulsion beyond hard clearance so traces
    *curve away* rather than hug minimum DRC.
    """
    if len(pts) < 3:
        return pts
    pts = [tuple(p) for p in pts]
    target = clearance_mm + 0.5 * width_mm + target_extra_mm

    for _ in range(iterations):
        forces = [(0.0, 0.0) for _ in pts]
        for i in range(1, len(pts) - 1):
            px, py = pts[i]
            # Neighbor springs (keep chain length reasonable)
            for j in (i - 1, i + 1):
                qx, qy = pts[j]
                dx, dy = qx - px, qy - py
                d = math.hypot(dx, dy) or 1e-9
                forces[i] = (forces[i][0] + 0.25 * dx, forces[i][1] + 0.25 * dy)
            # Obstacle keepouts
            for ob in om.obstacles.get(layer, []):
                if ob.net == net:
                    continue
                dx, dy = px - ob.cx, py - ob.cy
                d = math.hypot(dx, dy) or 1e-9
                r = 0.5 * max(ob.w, ob.h) + target
                if d < r * 2.2:
                    push = ((r - d) / r if d < r else 0.12 / d) * 0.7
                    forces[i] = (
                        forces[i][0] + push * dx / d,
                        forces[i][1] + push * dy / d,
                    )
            # Foreign painted copper (spacing field)
            for ps in om.painted.get(layer, []):
                if ps.net == net:
                    continue
                d = _point_seg_dist(px, py, ps.x1, ps.y1, ps.x2, ps.y2)
                need = clearance_mm + 0.5 * (width_mm + ps.width_mm) + target_extra_mm
                if d < need * 2.0:
                    mx, my = (ps.x1 + ps.x2) / 2, (ps.y1 + ps.y2) / 2
                    # Better normal: from closest point approx via midpoint + lateral
                    dx, dy = px - mx, py - my
                    # Prefer perpendicular to foreign segment
                    sx, sy = ps.x2 - ps.x1, ps.y2 - ps.y1
                    sl = math.hypot(sx, sy) or 1e-9
                    nx, ny = -sy / sl, sx / sl
                    # Choose normal pointing away from foreign mid
                    if dx * nx + dy * ny < 0:
                        nx, ny = -nx, -ny
                    push = (need - d) / need if d < need else 0.08
                    forces[i] = (
                        forces[i][0] + push * nx * 0.85,
                        forces[i][1] + push * ny * 0.85,
                    )
            # Mild smoothing toward arc (Laplacian)
            if 0 < i < len(pts) - 1:
                ax, ay = pts[i - 1]
                cx, cy = pts[i + 1]
                mx, my = (ax + cx) / 2, (ay + cy) / 2
                forces[i] = (
                    forces[i][0] + 0.2 * (mx - px),
                    forces[i][1] + 0.2 * (my - py),
                )

        new_pts = list(pts)
        for i in range(1, len(pts) - 1):
            fx, fy = forces[i]
            fn = math.hypot(fx, fy)
            if fn < 1e-9:
                continue
            sx = fx / fn * min(step, fn * step)
            sy = fy / fn * min(step, fn * step)
            nx, ny = pts[i][0] + sx, pts[i][1] + sy
            if not om.in_bounds(nx, ny) or om.blocked(nx, ny, layer, net):
                continue
            prev, nxt = pts[i - 1], pts[i + 1]
            if om.segment_blocked(prev[0], prev[1], nx, ny, layer, net, width_mm=width_mm):
                continue
            if om.segment_blocked(nx, ny, nxt[0], nxt[1], layer, net, width_mm=width_mm):
                continue
            new_pts[i] = (nx, ny)
        pts = new_pts
    return pts


def arc_approximate_corners(
    pts: list[tuple[float, float]],
    *,
    samples_per_corner: int = 3,
    min_turn_deg: float = 12.0,
    max_radius_frac: float = 0.35,
) -> tuple[list[tuple[float, float]], int]:
    """Replace sharp corners with circular-arc chord samples (TopoR free-angle look).

    Returns (new_polyline, number_of_corners_arced).
    Endpoints preserved; only intermediate corners with enough turn are rounded.
    """
    if len(pts) < 3 or samples_per_corner < 1:
        return pts, 0
    out: list[tuple[float, float]] = [pts[0]]
    arced = 0
    for i in range(1, len(pts) - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        v1 = (a[0] - b[0], a[1] - b[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        n1 = math.hypot(v1[0], v1[1]) or 1e-9
        n2 = math.hypot(v2[0], v2[1]) or 1e-9
        # Interior angle via dot of unit directions from corner
        u1 = (v1[0] / n1, v1[1] / n1)
        u2 = (v2[0] / n2, v2[1] / n2)
        dot = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
        turn = math.degrees(math.acos(dot))  # 0 = fold back, 180 = straight
        # Turn amount from collinear: |180 - turn|
        bend = abs(180.0 - turn)
        if bend < min_turn_deg:
            out.append(b)
            continue
        # Chord length limited by adjacent edge lengths
        r_max = max_radius_frac * min(n1, n2)
        if r_max < 0.15:
            out.append(b)
            continue
        # Sample points along a quadratic Bezier (arc-like) A→B→C shortcut
        # Control at B; pull samples off the sharp corner
        p0 = (
            b[0] + u1[0] * r_max,
            b[1] + u1[1] * r_max,
        )
        p2 = (
            b[0] + u2[0] * r_max,
            b[1] + u2[1] * r_max,
        )
        p1 = b  # control
        # Drop previous point if we already placed something very close to p0
        if out and _dist(out[-1], p0) < 0.05:
            pass
        else:
            # Don't duplicate if last is still far along edge
            if _dist(out[-1], p0) > 0.08:
                # walk from out[-1] is fine — just insert arc samples
                pass
        for k in range(1, samples_per_corner + 1):
            t = k / (samples_per_corner + 1)
            # Quadratic Bezier
            omt = 1 - t
            x = omt * omt * p0[0] + 2 * omt * t * p1[0] + t * t * p2[0]
            y = omt * omt * p0[1] + 2 * omt * t * p1[1] + t * t * p2[1]
            out.append((x, y))
        out.append(p2)
        arced += 1
    out.append(pts[-1])
    # Dedup
    cleaned = [out[0]]
    for p in out[1:]:
        if _dist(cleaned[-1], p) > 1e-5:
            cleaned.append(p)
    return cleaned, arced


def min_foreign_spacing_mm(
    result: RouteResult,
    *,
    sample_step_mm: float = 0.5,
) -> float:
    """Minimum centerline distance between different-net same-layer segments."""
    by_layer: dict[str, list[RouteSegment]] = {}
    for s in result.segments:
        by_layer.setdefault(s.layer, []).append(s)
    best = float("inf")
    for segs in by_layer.values():
        for i, a in enumerate(segs):
            length = max(_dist((a.x1, a.y1), (a.x2, a.y2)), 0.01)
            n = max(1, int(length / sample_step_mm))
            for k in range(n + 1):
                t = k / n
                px = a.x1 + (a.x2 - a.x1) * t
                py = a.y1 + (a.y2 - a.y1) * t
                for b in segs[i + 1 :]:
                    if a.net == b.net:
                        continue
                    d = _point_seg_dist(px, py, b.x1, b.y1, b.x2, b.y2)
                    # Edge-to-edge approx: centerline minus half-widths
                    edge = d - 0.5 * (a.width_mm + b.width_mm)
                    if edge < best:
                        best = edge
    return best if best < float("inf") else 999.0


@dataclass
class ToporGeometryMetrics:
    """TopoR-style scorecard after re-geometry."""

    total_length_mm: float = 0.0
    via_count: int = 0
    bend_count: int = 0
    multi_bend_nets: int = 0
    arc_corners: int = 0
    min_spacing_mm: float = 999.0
    segment_count: int = 0
    net_count_routed: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_length_mm": round(self.total_length_mm, 3),
            "via_count": self.via_count,
            "bend_count": self.bend_count,
            "multi_bend_nets": self.multi_bend_nets,
            "arc_corners": self.arc_corners,
            "min_spacing_mm": round(self.min_spacing_mm, 4)
            if self.min_spacing_mm < 900
            else None,
            "segment_count": self.segment_count,
            "net_count_routed": self.net_count_routed,
            "notes": self.notes,
        }


def compute_topor_geometry_metrics(result: RouteResult) -> ToporGeometryMetrics:
    """Aggregate bends / spacing / length for quality tests and UI."""
    m = ToporGeometryMetrics(
        via_count=result.via_count or len(result.vias),
        segment_count=len(result.segments),
    )
    total = 0.0
    by_net: dict[str, list[RouteSegment]] = {}
    for s in result.segments:
        total += _dist((s.x1, s.y1), (s.x2, s.y2))
        by_net.setdefault(s.net, []).append(s)
    m.total_length_mm = total
    m.net_count_routed = len(by_net)
    for net, segs in by_net.items():
        by_ly: dict[str, list[RouteSegment]] = {}
        for s in segs:
            by_ly.setdefault(s.layer, []).append(s)
        net_bends = 0
        for ly_segs in by_ly.values():
            pts = polyline_from_segments(ly_segs)
            net_bends += count_bends(pts)
        m.bend_count += net_bends
        if net_bends >= 1 or len(segs) >= 2:
            m.multi_bend_nets += 1
    m.min_spacing_mm = min_foreign_spacing_mm(result)
    return m


def post_connect_regeometry(
    result: RouteResult,
    board: BoardModel,
    *,
    clearance_mm: float = 0.2,
    iterations: int = 18,
    use_arcs: bool = True,
    max_seg_mm: float = 2.5,
    arc_samples: int = 3,
) -> RouteResult:
    """Full post-connect free-angle re-geometry pass.

    Topology (which nets connect, vias) stays fixed; geometry gains multi-bends,
    spacing, and optional arc-like corners.
    """
    if not result.segments:
        return result

    layers = sorted({s.layer for s in result.segments}) or list(
        board.copper_layers or ["F.Cu"]
    )
    om = build_obstacle_map(board, clearance_mm=clearance_mm, layers=layers)
    for s in result.segments:
        om.paint_trace(s.x1, s.y1, s.x2, s.y2, s.layer, s.width_mm, s.net)

    by_net: dict[str, list[RouteSegment]] = {}
    for s in result.segments:
        by_net.setdefault(s.net, []).append(s)

    new_segs: list[RouteSegment] = []
    total_len = 0.0
    total_arcs = 0
    chains_done = 0

    for net, segs in by_net.items():
        by_ly: dict[str, list[RouteSegment]] = {}
        for s in segs:
            by_ly.setdefault(s.layer, []).append(s)
        for ly, ly_segs in by_ly.items():
            width = ly_segs[0].width_mm
            # Continuous chains on this layer
            remaining = list(ly_segs)
            while remaining:
                chain = [remaining.pop(0)]
                changed = True
                while changed:
                    changed = False
                    for i, s in enumerate(remaining):
                        ends = {
                            (chain[0].x1, chain[0].y1),
                            (chain[0].x2, chain[0].y2),
                            (chain[-1].x1, chain[-1].y1),
                            (chain[-1].x2, chain[-1].y2),
                        }
                        s_ends = {(s.x1, s.y1), (s.x2, s.y2)}
                        if any(_dist(a, b) < 0.1 for a in ends for b in s_ends):
                            chain.append(remaining.pop(i))
                            changed = True
                            break
                pts = polyline_from_segments(chain)
                if len(pts) < 2:
                    continue
                # 1) multi-bend DOF
                pts = subdivide_polyline(pts, max_seg_mm=max_seg_mm)
                # 2) spacing + elastic-ish repel
                pts = spacing_repel_polyline(
                    pts,
                    ly,
                    net,
                    om,
                    width_mm=width,
                    clearance_mm=clearance_mm,
                    iterations=iterations,
                )
                # 3) optional arc-like corners
                if use_arcs and len(pts) >= 3:
                    pts, n_arc = arc_approximate_corners(
                        pts, samples_per_corner=arc_samples
                    )
                    total_arcs += n_arc
                    # Legality: drop illegal arc samples by reverting that corner
                    # cheap check: if any edge blocked, fall back to non-arc chain
                    illegal = False
                    for i in range(len(pts) - 1):
                        if om.segment_blocked(
                            pts[i][0],
                            pts[i][1],
                            pts[i + 1][0],
                            pts[i + 1][1],
                            ly,
                            net,
                            width_mm=width,
                        ):
                            illegal = True
                            break
                    if illegal:
                        pts = spacing_repel_polyline(
                            subdivide_polyline(
                                polyline_from_segments(chain), max_seg_mm=max_seg_mm
                            ),
                            ly,
                            net,
                            om,
                            width_mm=width,
                            clearance_mm=clearance_mm,
                            iterations=max(8, iterations // 2),
                        )
                        total_arcs = max(0, total_arcs - n_arc)

                for s in segments_from_polyline(pts, ly, net, width):
                    new_segs.append(s)
                    total_len += _dist((s.x1, s.y1), (s.x2, s.y2))
                    om.paint_trace(s.x1, s.y1, s.x2, s.y2, ly, width, net)
                chains_done += 1

    if not new_segs:
        return result

    out = RouteResult(
        segments=new_segs,
        vias=list(result.vias),
        via_count=result.via_count,
        total_length_mm=total_len,
        unrouted_nets=list(result.unrouted_nets),
        clearance_violations=result.clearance_violations,
        notes=list(result.notes)
        + [
            f"regeometry: post-connect free-angle · {chains_done} chain(s) · "
            f"arcs={total_arcs} · spacing+multi-bend"
        ],
        net_reports=list(result.net_reports),
        quality=dict(result.quality or {}),
    )
    metrics = compute_topor_geometry_metrics(out)
    metrics.arc_corners = total_arcs
    metrics.notes.append(
        f"TopoR metrics: bends={metrics.bend_count} multi_bend_nets={metrics.multi_bend_nets} "
        f"min_spacing={metrics.min_spacing_mm:.3f}mm arcs={total_arcs}"
    )
    out.quality = {
        **(out.quality or {}),
        "topor_geometry": metrics.to_dict(),
        "regeometry": {
            "chains": chains_done,
            "arc_corners": total_arcs,
            "use_arcs": use_arcs,
        },
    }
    out.notes.extend(metrics.notes[-2:])
    # Refresh net report lengths
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
    # Preserve geometry metrics after compute_quality may overwrite
    out.quality = {
        **(out.quality or {}),
        "topor_geometry": metrics.to_dict(),
        "regeometry": {
            "chains": chains_done,
            "arc_corners": total_arcs,
            "use_arcs": use_arcs,
        },
    }
    return out
