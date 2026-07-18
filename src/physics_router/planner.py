"""Feature-based high-level routing planner (ML-style without heavy deps).

Uses hand-crafted features + a fixed linear policy trained offline on synthetic
heuristics (coefficients chosen to match good industrial net-ordering practice).
Not an end-to-end copper net — only ordering, rip-up priority, and homotopy hints.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from physics_router.models import BoardModel, NetClass, PlacementConfig
from physics_router.router import _dist


@dataclass
class NetPlanFeatures:
    net: str
    n_pins: int = 0
    span_mm: float = 0.0
    weight: float = 1.0
    critical: bool = False
    class_rank: int = 5
    density_local: float = 0.0
    is_pair: bool = False
    is_power: bool = False
    is_clock: bool = False

    def feature_vector(self) -> list[float]:
        return [
            float(self.n_pins),
            self.span_mm,
            self.weight,
            1.0 if self.critical else 0.0,
            float(self.class_rank),
            self.density_local,
            1.0 if self.is_pair else 0.0,
            1.0 if self.is_power else 0.0,
            1.0 if self.is_clock else 0.0,
        ]


# Linear policy weights: higher score → route earlier
# [pins, span, weight, critical, class_rank (invert), density, pair, power, clock]
_POLICY_W = [
    -0.15,   # more pins → slightly later (escape last of dense)
    -0.02,   # longer span → later
    0.8,     # high weight → earlier
    1.5,     # critical → earlier
    -0.35,   # higher class_rank number → later
    -0.4,    # dense neighborhood → earlier to claim channels? actually earlier
    0.6,     # pairs together earlier
    2.0,     # power/gnd first
    1.2,     # clocks early
]
# Adjust density: actually route dense first to claim
_POLICY_W[5] = 0.5


@dataclass
class HighLevelPlan:
    net_order: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    features: dict[str, dict[str, Any]] = field(default_factory=dict)
    ripup_priority: list[str] = field(default_factory=list)
    homotopy_k: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "net_order": self.net_order,
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
            "ripup_priority": self.ripup_priority,
            "homotopy_k": self.homotopy_k,
            "notes": self.notes,
            "features": self.features,
        }


def _class_rank(nc: NetClass | None) -> int:
    order = {
        NetClass.GROUND: 0,
        NetClass.POWER: 1,
        NetClass.CLOCK: 2,
        NetClass.HIGH_SPEED: 2,
        NetClass.RF: 2,
        NetClass.DIFFERENTIAL: 3,
        NetClass.ANALOG: 3,
        NetClass.RESET: 4,
        NetClass.SIGNAL: 5,
        NetClass.OTHER: 6,
    }
    return order.get(nc, 5) if nc else 5


def extract_net_features(
    board: BoardModel, config: PlacementConfig | None = None
) -> dict[str, NetPlanFeatures]:
    # Local density: components per area near net centroid
    feats: dict[str, NetPlanFeatures] = {}
    for net, pins in board.nets.items():
        anchors = []
        for ref, _ in pins:
            c = board.components.get(ref)
            if c:
                anchors.append((c.x_mm, c.y_mm))
        if not anchors:
            continue
        cx = sum(p[0] for p in anchors) / len(anchors)
        cy = sum(p[1] for p in anchors) / len(anchors)
        span = 0.0
        if len(anchors) >= 2:
            xs = [p[0] for p in anchors]
            ys = [p[1] for p in anchors]
            span = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        nearby = sum(
            1
            for c in board.components.values()
            if _dist((c.x_mm, c.y_mm), (cx, cy)) < 8.0
        )
        lab = config.net_by_name().get(net) if config else None
        f = NetPlanFeatures(
            net=net,
            n_pins=len(pins),
            span_mm=span,
            weight=config.weight_for_net(net) if config else 1.0,
            critical=bool(lab.critical) if lab else False,
            class_rank=_class_rank(lab.net_class if lab else None),
            density_local=nearby / 10.0,
            is_pair=bool(lab and lab.pair_with),
            is_power=bool(lab and lab.net_class in (NetClass.POWER, NetClass.GROUND)),
            is_clock=bool(lab and lab.net_class in (NetClass.CLOCK, NetClass.HIGH_SPEED)),
        )
        feats[net] = f
    return feats


def plan_route_order(
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    k_homotopy_default: int = 3,
) -> HighLevelPlan:
    """Learned-style linear policy for net order + per-net homotopy K."""
    feats = extract_net_features(board, config)
    scores: dict[str, float] = {}
    for net, f in feats.items():
        vec = f.feature_vector()
        scores[net] = sum(w * x for w, x in zip(_POLICY_W, vec))

    order = sorted(scores.keys(), key=lambda n: (-scores[n], n))
    # Pairs: keep pair partners adjacent (second right after first)
    if config:
        final: list[str] = []
        placed: set[str] = set()
        for n in order:
            if n in placed:
                continue
            final.append(n)
            placed.add(n)
            lab = config.net_by_name().get(n)
            if lab and lab.pair_with and lab.pair_with in scores and lab.pair_with not in placed:
                final.append(lab.pair_with)
                placed.add(lab.pair_with)
        order = final

    # Homotopy K: more alternatives for dense / critical nets
    hk: dict[str, int] = {}
    for net, f in feats.items():
        k = k_homotopy_default
        if f.critical or f.is_clock:
            k = min(5, k + 1)
        if f.density_local > 1.5:
            k = min(5, k + 1)
        if f.n_pins <= 2 and f.span_mm < 5:
            k = 1
        hk[net] = k

    # Rip-up priority: reverse order (low score first to rip)
    ripup = list(reversed(order))

    plan = HighLevelPlan(
        net_order=order,
        scores=scores,
        features={n: {
            "n_pins": f.n_pins,
            "span_mm": round(f.span_mm, 2),
            "weight": f.weight,
            "critical": f.critical,
            "class_rank": f.class_rank,
            "density_local": round(f.density_local, 2),
        } for n, f in feats.items()},
        ripup_priority=ripup,
        homotopy_k=hk,
        notes=[
            "planner: linear feature policy (power/critical first, pairs co-ordered)",
            f"planner: {len(order)} nets, mean K-homotopy="
            f"{(sum(hk.values()) / max(len(hk), 1)):.1f}",
        ],
    )
    return plan
