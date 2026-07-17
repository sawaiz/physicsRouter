"""TopoR-inspired topological routing scaffolding.

Full free-angle rubberband routing is future work; this module provides:
- Unrouted problem extraction (nets + pad centers from placement)
- Simple straight-line / multi-segment free-angle guide paths
- Metrics for via count / length proxies used when ranking layouts
"""

from __future__ import annotations

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
class RouteResult:
    segments: list[RouteSegment] = field(default_factory=list)
    via_count: int = 0
    total_length_mm: float = 0.0
    unrouted_nets: list[str] = field(default_factory=list)


def pad_anchor(board: BoardModel, ref: str, _pad: str) -> tuple[float, float]:
    c = board.components[ref]
    return (c.x_mm, c.y_mm)


def topological_guide_route(
    board: BoardModel,
    config: PlacementConfig | None = None,
    preferred_layer: str = "F.Cu",
) -> RouteResult:
    """Connect each net with free-angle polyline through component centers.

    This is a topological *guide* (no DRC), useful as:
    - initial rubberband for a full TopoR-style engine
    - length/via proxy after placement
    """
    result = RouteResult()
    for net_name, pins in board.nets.items():
        anchors = []
        for ref, pad in pins:
            if ref not in board.components:
                continue
            anchors.append(pad_anchor(board, ref, pad))
        if len(anchors) < 2:
            result.unrouted_nets.append(net_name)
            continue
        # MST-like chain: nearest-neighbor tour from first pin
        remaining = anchors[1:]
        current = anchors[0]
        while remaining:
            nxt = min(remaining, key=lambda p: math.hypot(p[0] - current[0], p[1] - current[1]))
            remaining.remove(nxt)
            seg = RouteSegment(
                x1=current[0],
                y1=current[1],
                x2=nxt[0],
                y2=nxt[1],
                layer=preferred_layer,
                net=net_name,
            )
            length = math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)
            result.segments.append(seg)
            result.total_length_mm += length
            current = nxt
            # Prefer single-layer; count "via" when pair is differential? skip for now
    # Prefer fewer long crossings: crude via estimate for multi-layer later
    if config and config.board_width_mm:
        # density-based via proxy
        result.via_count = max(0, int(len(result.segments) * 0.05))
    return result
