"""Per-net K-homotopy alternatives.

Generate up to K topologically distinct free-angle paths for a connection,
deduplicated by TopologySignature rather than coordinate similarity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from physics_router.router import (
    ObstacleMap,
    free_angle_route,
    _dist,
)
from physics_router.topology import TopologySignature, radar_scan_points, signature_for_polyline


@dataclass
class HomotopyCandidate:
    points: list[tuple[float, float]]
    layer: str
    signature: TopologySignature
    length_mm: float
    method: str = ""
    cost: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "signature": self.signature.key(),
            "length_mm": round(self.length_mm, 4),
            "method": self.method,
            "cost": round(self.cost, 4),
            "n_points": len(self.points),
        }


def _path_length(pts: list[tuple[float, float]]) -> float:
    return sum(_dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _forced_side_detour(
    start: tuple[float, float],
    goal: tuple[float, float],
    om: ObstacleMap,
    layer: str,
    net: str,
    *,
    side: float,
    scale: float,
    grid_mm: float,
    width_mm: float,
) -> list[tuple[float, float]] | None:
    """Force a bulge on +side or -side of the chord (different homotopy)."""
    dx, dy = goal[0] - start[0], goal[1] - start[1]
    length = math.hypot(dx, dy) or 1.0
    px, py = -dy / length, dx / length
    mid = (
        (start[0] + goal[0]) / 2 + side * px * scale,
        (start[1] + goal[1]) / 2 + side * py * scale,
    )
    if not om.in_bounds(mid[0], mid[1]) or om.blocked(mid[0], mid[1], layer, net):
        return None
    if om.segment_blocked(start[0], start[1], mid[0], mid[1], layer, net, width_mm=width_mm):
        return None
    if om.segment_blocked(mid[0], mid[1], goal[0], goal[1], layer, net, width_mm=width_mm):
        return None
    return [start, mid, goal]


def k_homotopy_paths(
    start: tuple[float, float],
    goal: tuple[float, float],
    layer: str,
    net: str,
    om: ObstacleMap,
    *,
    k: int = 3,
    grid_mm: float = 0.5,
    width_mm: float = 0.25,
    congestion: Any | None = None,
) -> list[HomotopyCandidate]:
    """Return up to K topologically distinct paths on one layer.

    Strategies
    ----------
    1. Default free-angle (LOS / detour / radar / A*)
    2. Forced left/right bulges at several scales
    3. Radar portal two-corner chains from different portals
    """
    k = max(1, min(8, int(k)))
    found: list[HomotopyCandidate] = []
    seen_sig: set[str] = set()

    def _try(pts: list[tuple[float, float]] | None, method: str) -> None:
        if not pts or len(pts) < 2:
            return
        # Clearance gate
        for i in range(len(pts) - 1):
            if om.segment_blocked(
                pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1],
                layer, net, width_mm=width_mm,
            ):
                return
        sig = signature_for_polyline(net, layer, pts, om)
        # For homotopy uniqueness ignore net prefix differences — use obstacle sides
        sig_key = f"{layer}|{sig.via_count}|{','.join(sig.obstacle_sides)}"
        if sig_key in seen_sig:
            return
        seen_sig.add(sig_key)
        length = _path_length(pts)
        cost = length
        if congestion is not None:
            try:
                for i in range(len(pts) - 1):
                    cost = cost - length / max(len(pts) - 1, 1) + congestion.edge_cost(
                        pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], layer
                    )
            except Exception:
                pass
        found.append(
            HomotopyCandidate(
                points=pts,
                layer=layer,
                signature=sig,
                length_mm=length,
                method=method,
                cost=cost,
            )
        )

    # 1) Primary free-angle
    meth: list[str] = []
    primary = free_angle_route(
        start, goal, layer, net, om, grid_mm=grid_mm, width_mm=width_mm,
        method_out=meth, congestion=congestion,
    )
    _try(primary, meth[0] if meth else "primary")

    if len(found) >= k:
        return sorted(found, key=lambda c: c.cost)[:k]

    # 2) Forced side bulges (left/right homotopy)
    span = max(_dist(start, goal), 1.0)
    for side in (1.0, -1.0):
        for scale in (span * 0.25, span * 0.45, span * 0.7, grid_mm * 8, grid_mm * 14):
            if len(found) >= k:
                break
            pts = _forced_side_detour(
                start, goal, om, layer, net, side=side, scale=scale,
                grid_mm=grid_mm, width_mm=width_mm,
            )
            _try(pts, f"bulge_{'L' if side > 0 else 'R'}_{scale:.1f}")

    if len(found) >= k:
        return sorted(found, key=lambda c: c.cost)[:k]

    # 3) Radar portal chains
    portals = radar_scan_points(start, goal, om, layer, net, rays=16, grid_mm=grid_mm)
    for p in portals[:12]:
        if len(found) >= k:
            break
        if om.segment_blocked(start[0], start[1], p[0], p[1], layer, net, width_mm=width_mm):
            continue
        if om.segment_blocked(p[0], p[1], goal[0], goal[1], layer, net, width_mm=width_mm):
            continue
        _try([start, p, goal], "radar_portal")

    # 4) Two-portal combinations for more classes
    for i, a in enumerate(portals[:8]):
        for b in portals[i + 1 : 8]:
            if len(found) >= k:
                break
            ok = True
            chain = [start, a, b, goal]
            for j in range(len(chain) - 1):
                if om.segment_blocked(
                    chain[j][0], chain[j][1], chain[j + 1][0], chain[j + 1][1],
                    layer, net, width_mm=width_mm,
                ):
                    ok = False
                    break
            if ok:
                _try(chain, "radar2")

    return sorted(found, key=lambda c: c.cost)[:k]


def pick_best_homotopy(
    candidates: list[HomotopyCandidate],
    *,
    prefer_signature: str | None = None,
) -> HomotopyCandidate | None:
    if not candidates:
        return None
    if prefer_signature:
        for c in candidates:
            if c.signature.key() == prefer_signature or prefer_signature in c.signature.key():
                return c
    return min(candidates, key=lambda c: c.cost)
