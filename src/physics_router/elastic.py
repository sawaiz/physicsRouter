"""Continuous elastic-band geometry optimization.

Topology fixed: move intermediate vertices under shortening + obstacle
repulsion + spacing forces (Dayan/TopoR elastic geometry).
"""

from __future__ import annotations

import math
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


def _polyline_from_segs(segs: list[RouteSegment]) -> list[tuple[float, float]]:
    if not segs:
        return []
    pts = [(segs[0].x1, segs[0].y1)]
    for s in segs:
        last = pts[-1]
        if _dist(last, (s.x1, s.y1)) < 0.05:
            pts.append((s.x2, s.y2))
        elif _dist(last, (s.x2, s.y2)) < 0.05:
            pts.append((s.x1, s.y1))
        else:
            pts.append((s.x1, s.y1))
            pts.append((s.x2, s.y2))
    # dedup consecutive
    out = [pts[0]]
    for p in pts[1:]:
        if _dist(out[-1], p) > 1e-6:
            out.append(p)
    return out


def _segs_from_polyline(
    pts: list[tuple[float, float]], layer: str, net: str, width_mm: float
) -> list[RouteSegment]:
    segs: list[RouteSegment] = []
    for i in range(len(pts) - 1):
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


def elastic_optimize_polyline(
    pts: list[tuple[float, float]],
    layer: str,
    net: str,
    om: ObstacleMap,
    *,
    width_mm: float = 0.25,
    iterations: int = 24,
    step: float = 0.08,
    clearance_mm: float = 0.2,
) -> list[tuple[float, float]]:
    """Optimize intermediate vertices; endpoints fixed."""
    if len(pts) < 3:
        return pts
    pts = [tuple(p) for p in pts]
    # Insert midpoints on long segments for more DOF
    refined: list[tuple[float, float]] = [pts[0]]
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if _dist(a, b) > 4.0:
            refined.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        refined.append(b)
    pts = refined

    for _ in range(iterations):
        forces = [(0.0, 0.0) for _ in pts]
        # Shortening (elastic) toward neighbors
        for i in range(1, len(pts) - 1):
            px, py = pts[i]
            for j in (i - 1, i + 1):
                qx, qy = pts[j]
                dx, dy = qx - px, qy - py
                d = math.hypot(dx, dy) or 1e-9
                # spring strength
                k = 0.35
                forces[i] = (forces[i][0] + k * dx, forces[i][1] + k * dy)
            # Obstacle repulsion
            for ob in om.obstacles.get(layer, []):
                if ob.net == net:
                    continue
                dx, dy = px - ob.cx, py - ob.cy
                d = math.hypot(dx, dy) or 1e-9
                r = 0.5 * max(ob.w, ob.h) + clearance_mm + 0.5 * width_mm + 0.3
                if d < r * 2:
                    push = (r - d) / r if d < r else 0.15 / d
                    forces[i] = (
                        forces[i][0] + push * dx / d * 0.6,
                        forces[i][1] + push * dy / d * 0.6,
                    )
            # Painted foreign copper repulsion
            for ps in om.painted.get(layer, []):
                if ps.net == net:
                    continue
                d = _point_seg_dist(px, py, ps.x1, ps.y1, ps.x2, ps.y2)
                need = clearance_mm + 0.5 * (width_mm + ps.width_mm) + 0.2
                if d < need * 1.5:
                    # Approximate normal from closest point
                    # push away from segment midpoint as cheap normal
                    mx, my = (ps.x1 + ps.x2) / 2, (ps.y1 + ps.y2) / 2
                    dx, dy = px - mx, py - my
                    dn = math.hypot(dx, dy) or 1e-9
                    push = (need - d) / need if d < need else 0.05
                    forces[i] = (
                        forces[i][0] + push * dx / dn * 0.5,
                        forces[i][1] + push * dy / dn * 0.5,
                    )
            # Curvature penalty: smooth turn
            if 0 < i < len(pts) - 1:
                ax, ay = pts[i - 1]
                cx, cy = pts[i + 1]
                mx, my = (ax + cx) / 2, (ay + cy) / 2
                forces[i] = (
                    forces[i][0] + 0.15 * (mx - px),
                    forces[i][1] + 0.15 * (my - py),
                )

        # Apply forces with legality check
        new_pts = list(pts)
        for i in range(1, len(pts) - 1):
            fx, fy = forces[i]
            fn = math.hypot(fx, fy)
            if fn < 1e-9:
                continue
            # limit step
            sx = fx / fn * min(step, fn * step)
            sy = fy / fn * min(step, fn * step)
            nx, ny = pts[i][0] + sx, pts[i][1] + sy
            if not om.in_bounds(nx, ny):
                continue
            if om.blocked(nx, ny, layer, net):
                continue
            # edges to neighbors must stay clear
            prev, nxt = pts[i - 1], pts[i + 1]
            if om.segment_blocked(prev[0], prev[1], nx, ny, layer, net, width_mm=width_mm):
                continue
            if om.segment_blocked(nx, ny, nxt[0], nxt[1], layer, net, width_mm=width_mm):
                continue
            new_pts[i] = (nx, ny)
        pts = new_pts

    # Drop nearly collinear intermediates
    cleaned = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = cleaned[-1], pts[i], pts[i + 1]
        ab = _dist(a, b)
        bc = _dist(b, c)
        ac = _dist(a, c)
        if ab + bc > ac * 1.02:  # not collinear enough to drop
            # keep if needed for clearance
            if om.segment_blocked(a[0], a[1], c[0], c[1], layer, net, width_mm=width_mm):
                cleaned.append(b)
            elif ab + bc - ac > 0.05:
                cleaned.append(b)
        else:
            cleaned.append(b)
    cleaned.append(pts[-1])
    return cleaned


def elastic_optimize_route(
    result: RouteResult,
    board: BoardModel,
    *,
    clearance_mm: float = 0.2,
    iterations: int = 20,
) -> RouteResult:
    """Apply elastic optimization per net/layer continuous chain."""
    layers = sorted({s.layer for s in result.segments}) or list(board.copper_layers or ["F.Cu"])
    om = build_obstacle_map(board, clearance_mm=clearance_mm, layers=layers)
    # Paint all copper first
    for s in result.segments:
        om.paint_trace(s.x1, s.y1, s.x2, s.y2, s.layer, s.width_mm, s.net)

    by_net: dict[str, list[RouteSegment]] = {}
    for s in result.segments:
        by_net.setdefault(s.net, []).append(s)

    new_segs: list[RouteSegment] = []
    total = 0.0
    moved = 0
    for net, segs in by_net.items():
        by_ly: dict[str, list[RouteSegment]] = {}
        for s in segs:
            by_ly.setdefault(s.layer, []).append(s)
        for ly, ly_segs in by_ly.items():
            width = ly_segs[0].width_mm
            # Group into continuous chains
            remaining = list(ly_segs)
            chains: list[list[RouteSegment]] = []
            while remaining:
                chain = [remaining.pop(0)]
                changed = True
                while changed:
                    changed = False
                    for i, s in enumerate(remaining):
                        ends = {(chain[0].x1, chain[0].y1), (chain[0].x2, chain[0].y2),
                                (chain[-1].x1, chain[-1].y1), (chain[-1].x2, chain[-1].y2)}
                        s_ends = {(s.x1, s.y1), (s.x2, s.y2)}
                        if any(_dist(a, b) < 0.08 for a in ends for b in s_ends):
                            chain.append(remaining.pop(i))
                            changed = True
                            break
                chains.append(chain)

            for chain in chains:
                pts = _polyline_from_segs(chain)
                before = list(pts)
                pts2 = elastic_optimize_polyline(
                    pts, ly, net, om, width_mm=width,
                    iterations=iterations, clearance_mm=clearance_mm,
                )
                if pts2 != before:
                    moved += 1
                # Unpaint old, paint new
                for s in chain:
                    # cannot unpaint easily — paint new as same net (OK)
                    pass
                for s in _segs_from_polyline(pts2, ly, net, width):
                    new_segs.append(s)
                    total += _dist((s.x1, s.y1), (s.x2, s.y2))
                    om.paint_trace(s.x1, s.y1, s.x2, s.y2, ly, width, net)

    if not new_segs:
        return result

    out = RouteResult(
        segments=new_segs,
        vias=list(result.vias),
        via_count=result.via_count,
        total_length_mm=total,
        unrouted_nets=list(result.unrouted_nets),
        clearance_violations=result.clearance_violations,
        notes=list(result.notes) + [f"elastic: optimized {moved} chain(s), {iterations} iters"],
        net_reports=list(result.net_reports),
        quality=dict(result.quality or {}),
    )
    # refresh lengths
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
