"""Signal-integrity and manufacturing cost terms for routing score.

Lightweight analytical estimators (not full-wave). Used by the high-level
planner, multi-variant ranking, and explainable quality reports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from physics_router.models import BoardModel, NetClass, PlacementConfig
from physics_router.router import RouteResult, RouteSegment, _dist, _point_seg_dist


@dataclass
class SiMfgCosts:
    """Separate SI and manufacturing risk components (minimize)."""

    # SI proxies
    parallel_run_mm: float = 0.0  # same-layer foreign nets running close
    layer_hop_penalty: float = 0.0  # via count weighted by net criticality
    return_path_proxy: float = 0.0  # signal without nearby ground net copper
    length_excess_mm: float = 0.0  # vs HPWL lower bound
    pair_skew_mm: float = 0.0  # differential / pair length mismatch

    # Manufacturing proxies
    acute_angle_count: int = 0
    short_stub_count: int = 0
    via_near_pad_count: int = 0
    tiny_segment_count: int = 0
    copper_density_imbalance: float = 0.0  # |F-B| / total
    neck_risk: float = 0.0  # thin track next to dense copper

    notes: list[str] = field(default_factory=list)

    def total_si(self) -> float:
        return (
            self.parallel_run_mm * 0.15
            + self.layer_hop_penalty * 2.0
            + self.return_path_proxy * 1.5
            + self.length_excess_mm * 0.05
            + self.pair_skew_mm * 0.4
        )

    def total_mfg(self) -> float:
        return (
            self.acute_angle_count * 1.2
            + self.short_stub_count * 0.8
            + self.via_near_pad_count * 1.5
            + self.tiny_segment_count * 0.3
            + self.copper_density_imbalance * 8.0
            + self.neck_risk * 2.0
        )

    def total(self) -> float:
        return self.total_si() + self.total_mfg()

    def to_dict(self) -> dict[str, Any]:
        return {
            "si": {
                "parallel_run_mm": round(self.parallel_run_mm, 3),
                "layer_hop_penalty": round(self.layer_hop_penalty, 3),
                "return_path_proxy": round(self.return_path_proxy, 3),
                "length_excess_mm": round(self.length_excess_mm, 3),
                "pair_skew_mm": round(self.pair_skew_mm, 3),
                "total": round(self.total_si(), 3),
            },
            "mfg": {
                "acute_angle_count": self.acute_angle_count,
                "short_stub_count": self.short_stub_count,
                "via_near_pad_count": self.via_near_pad_count,
                "tiny_segment_count": self.tiny_segment_count,
                "copper_density_imbalance": round(self.copper_density_imbalance, 3),
                "neck_risk": round(self.neck_risk, 3),
                "total": round(self.total_mfg(), 3),
            },
            "combined": round(self.total(), 3),
            "notes": self.notes[:12],
        }


def _angle_deg(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    """Interior angle at B for polyline A-B-C, degrees."""
    v1x, v1y = ax - bx, ay - by
    v2x, v2y = cx - bx, cy - by
    n1 = math.hypot(v1x, v1y) or 1e-9
    n2 = math.hypot(v2x, v2y) or 1e-9
    cos_a = max(-1.0, min(1.0, (v1x * v2x + v1y * v2y) / (n1 * n2)))
    return math.degrees(math.acos(cos_a))


def evaluate_si_mfg(
    result: RouteResult,
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    clearance_mm: float = 0.2,
    parallel_gap_mm: float | None = None,
) -> SiMfgCosts:
    """Compute SI + manufacturing proxies for a complete route."""
    costs = SiMfgCosts()
    gap = parallel_gap_mm if parallel_gap_mm is not None else max(clearance_mm * 3.0, 0.5)

    # --- Parallel run exposure (crosstalk proxy) ---
    by_layer: dict[str, list[RouteSegment]] = {}
    for s in result.segments:
        by_layer.setdefault(s.layer, []).append(s)
    for _ly, segs in by_layer.items():
        for i, a in enumerate(segs):
            for b in segs[i + 1 :]:
                if a.net == b.net:
                    continue
                # Approximate parallel length when midpoints are close and directions align
                amx, amy = (a.x1 + a.x2) / 2, (a.y1 + a.y2) / 2
                d = _point_seg_dist(amx, amy, b.x1, b.y1, b.x2, b.y2)
                if d > gap:
                    continue
                adx, ady = a.x2 - a.x1, a.y2 - a.y1
                bdx, bdy = b.x2 - b.x1, b.y2 - b.y1
                al = math.hypot(adx, ady) or 1e-9
                bl = math.hypot(bdx, bdy) or 1e-9
                cos_t = abs((adx * bdx + ady * bdy) / (al * bl))
                if cos_t > 0.85:  # nearly parallel
                    costs.parallel_run_mm += min(al, bl) * (1.0 - d / gap)

    # --- Layer hops / via penalty by criticality ---
    via_by_net: dict[str, int] = {}
    for v in result.vias:
        via_by_net[v.net] = via_by_net.get(v.net, 0) + 1
    for net, n_v in via_by_net.items():
        w = 1.0
        if config:
            lab = config.net_by_name().get(net)
            if lab:
                if lab.net_class in (NetClass.HIGH_SPEED, NetClass.DIFFERENTIAL, NetClass.RF):
                    w = 2.5
                elif lab.net_class in (NetClass.CLOCK,):
                    w = 2.0
                elif lab.critical:
                    w = 1.8
        costs.layer_hop_penalty += n_v * w

    # --- Length excess vs HPWL ---
    for net, pins in board.nets.items():
        anchors = []
        for ref, _ in pins:
            c = board.components.get(ref)
            if c:
                anchors.append((c.x_mm, c.y_mm))
        if len(anchors) < 2:
            continue
        # MST lower bound approx: sum of consecutive after sort by x
        xs = sorted(anchors, key=lambda p: (p[0], p[1]))
        hpwl = sum(_dist(xs[i], xs[i + 1]) for i in range(len(xs) - 1))
        net_len = sum(
            _dist((s.x1, s.y1), (s.x2, s.y2)) for s in result.segments if s.net == net
        )
        if net_len > hpwl * 1.05:
            costs.length_excess_mm += net_len - hpwl

    # --- Pair skew ---
    if config:
        seen: set[str] = set()
        for lab in config.nets:
            if not lab.pair_with or lab.name in seen:
                continue
            a, b = lab.name, lab.pair_with
            if a not in board.nets or b not in board.nets:
                continue
            seen.add(a)
            seen.add(b)
            la = sum(_dist((s.x1, s.y1), (s.x2, s.y2)) for s in result.segments if s.net == a)
            lb = sum(_dist((s.x1, s.y1), (s.x2, s.y2)) for s in result.segments if s.net == b)
            if la > 0 and lb > 0:
                costs.pair_skew_mm += abs(la - lb)

    # --- Return path proxy: outer-layer signal without nearby GND copper ---
    gnd_segs = [
        s
        for s in result.segments
        if config
        and (lab := config.net_by_name().get(s.net))
        and lab.net_class == NetClass.GROUND
    ]
    if gnd_segs:
        signal_mm = 0.0
        orphan_mm = 0.0
        for s in result.segments:
            if config:
                lab = config.net_by_name().get(s.net)
                if lab and lab.net_class in (NetClass.GROUND, NetClass.POWER):
                    continue
            sl = _dist((s.x1, s.y1), (s.x2, s.y2))
            signal_mm += sl
            mx, my = (s.x1 + s.x2) / 2, (s.y1 + s.y2) / 2
            near = any(
                _point_seg_dist(mx, my, g.x1, g.y1, g.x2, g.y2) < 3.0 for g in gnd_segs
            )
            if not near:
                orphan_mm += sl
        if signal_mm > 0:
            costs.return_path_proxy = orphan_mm / signal_mm * 10.0

    # --- MFG: acute angles, stubs, tiny segments ---
    by_net_layer: dict[tuple[str, str], list[RouteSegment]] = {}
    for s in result.segments:
        by_net_layer.setdefault((s.net, s.layer), []).append(s)
        sl = _dist((s.x1, s.y1), (s.x2, s.y2))
        if sl < 0.15:
            costs.tiny_segment_count += 1
        if sl < 0.35 and s.width_mm < 0.2:
            costs.short_stub_count += 1

    for (_net, _ly), segs in by_net_layer.items():
        # Chain collinear-ish polylines by endpoint match
        for s in segs:
            for t in segs:
                if s is t:
                    continue
                # angle at shared vertex
                for (px, py), (ax, ay), (cx, cy) in (
                    ((s.x2, s.y2), (s.x1, s.y1), (t.x2, t.y2)),
                    ((s.x1, s.y1), (s.x2, s.y2), (t.x1, t.y1)),
                ):
                    if _dist((px, py), (t.x1, t.y1)) < 0.05 or _dist((px, py), (t.x2, t.y2)) < 0.05:
                        if _dist((px, py), (t.x1, t.y1)) < 0.05:
                            cx, cy = t.x2, t.y2
                        else:
                            cx, cy = t.x1, t.y1
                        ang = _angle_deg(ax, ay, px, py, cx, cy)
                        if 5 < ang < 75:  # sharp acute turn (not 180 collinear)
                            costs.acute_angle_count += 1

    # --- Via near pad ---
    for v in result.vias:
        for c in board.components.values():
            if _dist((v.x, v.y), (c.x_mm, c.y_mm)) < max(c.width_mm, c.height_mm, 0.8) * 0.6:
                costs.via_near_pad_count += 1
                break

    # --- Copper density imbalance F vs B ---
    f_len = sum(
        _dist((s.x1, s.y1), (s.x2, s.y2))
        for s in result.segments
        if s.layer.startswith("F.")
    )
    b_len = sum(
        _dist((s.x1, s.y1), (s.x2, s.y2))
        for s in result.segments
        if s.layer.startswith("B.")
    )
    tot = f_len + b_len
    if tot > 1e-6:
        costs.copper_density_imbalance = abs(f_len - b_len) / tot

    # --- Neck risk: thin tracks near foreign copper ---
    for s in result.segments:
        if s.width_mm >= 0.3:
            continue
        mx, my = (s.x1 + s.x2) / 2, (s.y1 + s.y2) / 2
        for o in by_layer.get(s.layer, []):
            if o.net == s.net:
                continue
            if _point_seg_dist(mx, my, o.x1, o.y1, o.x2, o.y2) < clearance_mm * 2.5:
                costs.neck_risk += 0.25
                break

    if costs.parallel_run_mm > 5:
        costs.notes.append(f"crosstalk_proxy: {costs.parallel_run_mm:.1f} mm parallel exposure")
    if costs.acute_angle_count:
        costs.notes.append(f"mfg: {costs.acute_angle_count} acute copper angles")
    if costs.via_near_pad_count:
        costs.notes.append(f"mfg: {costs.via_near_pad_count} via(s) near pads")
    if costs.pair_skew_mm > 0.5:
        costs.notes.append(f"si: pair skew {costs.pair_skew_mm:.2f} mm")
    if costs.return_path_proxy > 3:
        costs.notes.append("si: weak return-path proximity on some signals")

    return costs


def edge_si_mfg_penalty(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    layer: str,
    net: str,
    *,
    width_mm: float = 0.25,
    via: bool = False,
) -> float:
    """Cheap incremental penalty for path search (not full board analysis)."""
    length = _dist((x1, y1), (x2, y2))
    pen = 0.0
    # Prefer slightly wider effective clearance budget on outer signal layers
    if layer.startswith("In"):
        pen += 0.02 * length  # mild inner-layer preference for power-ish
    if via:
        pen += 3.0
    # Short jogs are manufacturability risk
    if 0 < length < 0.2:
        pen += 0.5
    # Very thin + long: neck risk proxy
    if width_mm < 0.2 and length > 15:
        pen += 0.1 * length
    return pen
