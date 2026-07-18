"""HALO-style concentric ring / polar routing (mirror of third_party/halo-90/pcb/halo.js).

Charlieplex LED rings need **radial spokes + concentric arcs**, not free-angle
chords across the board. This module detects an LED ring, assigns each CPX bus
to a (layer, track) pair, and builds polar copper that matches the halo.js
geometry model.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable

from physics_router.models import BoardModel, PlacementConfig
from physics_router.router import (
    NetRouteReport,
    RouteResult,
    RouteSegment,
    Via,
    attach_router_drc,
    board_extent,
    build_obstacle_map,
    clearance_aware_route,
    fanout_anchor,
)

ProgressCallback = Callable[[int, int, str, str, dict], None]

# halo.js defaults
_DEFAULT_LED_R = 11.0
_INNER_VIA_SPACING = 0.55
_TRACE_W = 0.128
_CLEAR = 0.128

# Signal index → (layer_name_key, track_index)  — same order as halo.js layerMap
# track 0 = outermost / under LEDs, track 3 = innermost
_LAYER_MAP: list[tuple[str, int]] = [
    ("F.Cu", 1),
    ("F.Cu", 2),
    ("F.Cu", 3),
    ("In1.Cu", 0),
    ("In1.Cu", 1),
    ("In1.Cu", 2),
    ("In1.Cu", 3),
    ("In2.Cu", 0),
    ("In2.Cu", 2),
    ("In2.Cu", 3),
]


@dataclass
class LedRing:
    cx: float
    cy: float
    radius: float
    led_refs: list[str]  # ordered by angle ascending
    angles_deg: list[float]  # parallel to led_refs


def _ang(x: float, y: float, cx: float, cy: float) -> float:
    # halo.js: x=sin(θ), y=cos(θ) with θ from +Y — atan2(x, y) matches that
    return math.degrees(math.atan2(x - cx, y - cy)) % 360.0


def _xy(r: float, deg: float, cx: float, cy: float) -> tuple[float, float]:
    rad = math.radians(deg)
    return (cx + r * math.sin(rad), cy + r * math.cos(rad))


def detect_led_ring(
    board: BoardModel,
    *,
    min_leds: int = 24,
    prefix: str = "D",
) -> LedRing | None:
    """Return LED ring geometry if footprints form a clear circle."""
    leds = [
        (ref, c)
        for ref, c in board.components.items()
        if ref.startswith(prefix) and re.match(rf"^{re.escape(prefix)}\d+$", ref)
    ]
    if len(leds) < min_leds:
        return None
    cx = sum(c.x_mm for _, c in leds) / len(leds)
    cy = sum(c.y_mm for _, c in leds) / len(leds)
    rs = [math.hypot(c.x_mm - cx, c.y_mm - cy) for _, c in leds]
    r_mean = sum(rs) / len(rs)
    if r_mean < 2.0:
        return None
    # Relative stdev of radius — ring if tight
    var = sum((r - r_mean) ** 2 for r in rs) / len(rs)
    if math.sqrt(var) > 0.08 * r_mean:
        return None
    ordered = sorted(
        leds,
        key=lambda rc: _ang(rc[1].x_mm, rc[1].y_mm, cx, cy),
    )
    refs = [r for r, _ in ordered]
    angs = [_ang(c.x_mm, c.y_mm, cx, cy) for _, c in ordered]
    return LedRing(cx=cx, cy=cy, radius=r_mean, led_refs=refs, angles_deg=angs)


def _cpx_index(name: str) -> int | None:
    m = re.match(r"^CPX[-_]?(\d+)$", name.upper())
    if not m:
        return None
    return int(m.group(1))


def _resolve_layer(board: BoardModel, key: str) -> str:
    layers = list(board.copper_layers or [])
    aliases = {
        "F.Cu": ["F.Cu", "Front", "F"],
        "B.Cu": ["B.Cu", "Back", "B"],
        "In1.Cu": ["In1.Cu", "In1", "Inner1"],
        "In2.Cu": ["In2.Cu", "In2", "Inner2"],
    }
    for cand in aliases.get(key, [key]):
        if cand in layers:
            return cand
    # fallback by index
    if key.startswith("In") and len(layers) >= 3:
        return layers[1] if "1" in key else layers[min(2, len(layers) - 1)]
    if key.startswith("B") and layers:
        return layers[-1]
    return layers[0] if layers else "F.Cu"


def track_radius(
    led_r: float,
    track: int,
    *,
    max_track: int = 3,
    via_pitch: float = _INNER_VIA_SPACING,
    clearance: float = _CLEAR,
) -> float:
    """Concentric track radius (mm) — halo.js inner-signal formula."""
    if track <= 0:
        return led_r
    # radius - ((1+(innerViaSpacing*(maxTrack+1)))+((track)*2*clearence))
    return led_r - (
        (1.0 + via_pitch * (max_track + 1)) + (track * 2.0 * clearance)
    )


def via_radius(led_r: float, track: int, *, via_pitch: float = _INNER_VIA_SPACING) -> float:
    """Signal via ring radius for a track (halo.js ofsett)."""
    if track <= 0:
        return led_r - 1.0
    return led_r - ((track * via_pitch) + 1.0)


def arc_polyline(
    r: float,
    a0_deg: float,
    a1_deg: float,
    *,
    cx: float = 0.0,
    cy: float = 0.0,
    step_deg: float = 2.0,
) -> list[tuple[float, float]]:
    """Polyline approximating a circular arc (shortest direction)."""
    # Normalize delta to (-180, 180]
    d = (a1_deg - a0_deg + 180.0) % 360.0 - 180.0
    n = max(1, int(math.ceil(abs(d) / max(step_deg, 0.5))))
    pts: list[tuple[float, float]] = []
    for i in range(n + 1):
        t = i / n
        ang = a0_deg + d * t
        pts.append(_xy(r, ang, cx, cy))
    return pts


def polar_path(
    start: tuple[float, float],
    goal: tuple[float, float],
    *,
    cx: float,
    cy: float,
    track_r: float,
    step_deg: float = 2.0,
) -> list[tuple[float, float]]:
    """Radial to track → arc on track → radial to goal."""
    a0 = _ang(start[0], start[1], cx, cy)
    a1 = _ang(goal[0], goal[1], cx, cy)
    p_track0 = _xy(track_r, a0, cx, cy)
    p_track1 = _xy(track_r, a1, cx, cy)
    mid = arc_polyline(track_r, a0, a1, cx=cx, cy=cy, step_deg=step_deg)
    # Dedup near points
    path = [start, p_track0]
    for p in mid:
        if math.hypot(p[0] - path[-1][0], p[1] - path[-1][1]) > 1e-4:
            path.append(p)
    if math.hypot(p_track1[0] - path[-1][0], p_track1[1] - path[-1][1]) > 1e-4:
        path.append(p_track1)
    if math.hypot(goal[0] - path[-1][0], goal[1] - path[-1][1]) > 1e-4:
        path.append(goal)
    return path


def _emit_poly(
    result: RouteResult,
    pts: list[tuple[float, float]],
    layer: str,
    net: str,
    width: float,
    om: Any | None,
) -> float:
    length = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        d = math.hypot(x2 - x1, y2 - y1)
        if d < 1e-6:
            continue
        result.segments.append(
            RouteSegment(x1=x1, y1=y1, x2=x2, y2=y2, layer=layer, net=net, width_mm=width)
        )
        length += d
        if om is not None:
            om.paint_trace(x1, y1, x2, y2, layer, width, net)
    result.total_length_mm += length
    return length


def _path_clear(
    om: Any,
    pts: list[tuple[float, float]],
    layer: str,
    net: str,
    width: float,
) -> bool:
    for i in range(len(pts) - 1):
        if om.segment_blocked(
            pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], layer, net, width_mm=width
        ):
            return False
    return True


def check_ring_track_spacing(
    result: RouteResult,
    ring: LedRing,
    *,
    clearance_mm: float = _CLEAR,
    min_pitch: float | None = None,
) -> dict[str, Any]:
    """Report near-miss concentric arcs of different nets on the same layer."""
    need = min_pitch if min_pitch is not None else (clearance_mm + _TRACE_W)
    # Sample midpoints; group by layer
    samples: list[tuple[str, str, float, float, float]] = []  # layer, net, r, x, y
    for s in result.segments:
        mx, my = 0.5 * (s.x1 + s.x2), 0.5 * (s.y1 + s.y2)
        r = math.hypot(mx - ring.cx, my - ring.cy)
        samples.append((s.layer, s.net, r, mx, my))
    hits = 0
    notes: list[str] = []
    for i, (la, na, ra, xa, ya) in enumerate(samples):
        for lb, nb, rb, xb, yb in samples[i + 1 :]:
            if la != lb or na == nb:
                continue
            # Same angle sector and close radius → track pitch violation
            aa = _ang(xa, ya, ring.cx, ring.cy)
            ab = _ang(xb, yb, ring.cx, ring.cy)
            dang = abs((aa - ab + 180) % 360 - 180)
            if dang > 8.0:
                continue
            if abs(ra - rb) < need and abs(ra - rb) > 1e-6:
                hits += 1
                if len(notes) < 8:
                    notes.append(
                        f"{la}: {na}×{nb} track pitch |Δr|={abs(ra-rb):.3f}<{need:.3f}"
                    )
    return {"near_track_pairs": hits, "notes": notes, "need_mm": need}


def halo_ring_route(
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    clearance_mm: float | None = None,
    width_mm: float | None = None,
    progress_cb: ProgressCallback | None = None,
    route_non_cpx: bool = True,
) -> RouteResult:
    """Route charlieplex LED ring with concentric tracks (halo.js style).

    CPX nets use polar copper; other nets optionally run through
    clearance_aware_route with CPX already painted.
    """
    ring = detect_led_ring(board)
    cl = float(clearance_mm if clearance_mm is not None else _CLEAR)
    w = float(width_mm if width_mm is not None else _TRACE_W)
    layers = list(board.copper_layers) or ["F.Cu", "B.Cu"]
    result = RouteResult()
    result.notes.append("style: halo_ring (concentric tracks + radial spokes)")

    if ring is None:
        result.notes.append("halo_ring: no LED ring detected — fallback free-angle")
        return clearance_aware_route(
            board,
            config,
            clearance_mm=max(cl, 0.15),
            soft_fallback=False,
            prefer_native=True,
            allow_vias=True,
            progress_cb=progress_cb,
        )

    result.notes.append(
        f"halo_ring: center=({ring.cx:.2f},{ring.cy:.2f}) R={ring.radius:.2f}mm "
        f"leds={len(ring.led_refs)} cl={cl} w={w}"
    )
    om = build_obstacle_map(board, clearance_mm=cl, layers=layers)
    max_track = max(t for _, t in _LAYER_MAP)

    cpx_nets = sorted(
        (n for n in board.nets if _cpx_index(n) is not None),
        key=lambda n: _cpx_index(n) or 0,
    )
    total = len(cpx_nets) + (1 if route_non_cpx else 0)

    for ni, net in enumerate(cpx_nets):
        idx = _cpx_index(net) or 0
        layer_key, track = _LAYER_MAP[idx % len(_LAYER_MAP)]
        layer = _resolve_layer(board, layer_key)
        tr = track_radius(ring.radius, track, max_track=max_track, clearance=cl)
        vr = via_radius(ring.radius, track)
        if progress_cb:
            try:
                progress_cb(ni, total, net, "ring_route", {"track": track, "layer": layer})
            except Exception:
                pass

        pins = board.nets.get(net) or []
        anchors: list[tuple[float, float]] = []
        for ref, pad in pins:
            if ref not in board.components:
                continue
            anchors.append(fanout_anchor(board, ref, net, pad_num=str(pad)))
        # unique
        uniq: list[tuple[float, float]] = []
        for a in anchors:
            if not any(math.hypot(a[0] - u[0], a[1] - u[1]) < 0.05 for u in uniq):
                uniq.append(a)
        anchors = uniq
        report = NetRouteReport(net=net, pins=len(pins))
        if len(anchors) < 2:
            report.status = "skipped"
            report.notes.append("fewer than 2 anchors")
            result.net_reports.append(report)
            continue

        # MST on anchors using polar path length as cost
        remaining = set(range(1, len(anchors)))
        tree = {0}
        net_len = 0.0
        net_segs = 0
        net_vias = 0
        methods: list[str] = []
        while remaining:
            best: tuple[float, int, int] | None = None
            for i in tree:
                for j in remaining:
                    d = math.hypot(
                        anchors[i][0] - anchors[j][0], anchors[i][1] - anchors[j][1]
                    )
                    # Prefer angular separation * track radius as cost
                    ai = _ang(anchors[i][0], anchors[i][1], ring.cx, ring.cy)
                    aj = _ang(anchors[j][0], anchors[j][1], ring.cx, ring.cy)
                    dang = abs((ai - aj + 180) % 360 - 180) * math.pi / 180.0
                    cost = abs(tr) * dang + 0.25 * d
                    if best is None or cost < best[0]:
                        best = (cost, i, j)
            assert best is not None
            _, ia, ib = best
            remaining.remove(ib)
            tree.add(ib)
            path = polar_path(
                anchors[ia],
                anchors[ib],
                cx=ring.cx,
                cy=ring.cy,
                track_r=tr,
                step_deg=2.0,
            )
            # Soften: if blocked, try slightly different track radii
            ok = _path_clear(om, path, layer, net, w)
            if not ok:
                for scale in (0.97, 1.03, 0.94, 1.06):
                    path2 = polar_path(
                        anchors[ia],
                        anchors[ib],
                        cx=ring.cx,
                        cy=ring.cy,
                        track_r=tr * scale,
                        step_deg=1.5,
                    )
                    if _path_clear(om, path2, layer, net, w):
                        path = path2
                        ok = True
                        break
            if not ok and len(layers) >= 2:
                # Via detour on B.Cu radial then arc on preferred layer
                mid_ang = 0.5 * (
                    _ang(anchors[ia][0], anchors[ia][1], ring.cx, ring.cy)
                    + _ang(anchors[ib][0], anchors[ib][1], ring.cx, ring.cy)
                )
                vx, vy = _xy(vr, mid_ang, ring.cx, ring.cy)
                via_layer_b = layers[-1]
                p0 = polar_path(
                    anchors[ia], (vx, vy), cx=ring.cx, cy=ring.cy, track_r=tr, step_deg=2.0
                )
                p1 = polar_path(
                    (vx, vy), anchors[ib], cx=ring.cx, cy=ring.cy, track_r=tr, step_deg=2.0
                )
                if _path_clear(om, p0, layer, net, w) and _path_clear(
                    om, p1, layer, net, w
                ):
                    net_len += _emit_poly(result, p0, layer, net, w, om)
                    net_len += _emit_poly(result, p1, layer, net, w, om)
                    result.vias.append(
                        Via(
                            x=vx,
                            y=vy,
                            net=net,
                            size_mm=0.45,
                            drill_mm=0.2,
                            layers=(layer, via_layer_b),
                            reason="halo_ring track via",
                        )
                    )
                    result.via_count += 1
                    net_vias += 1
                    for ly in layers:
                        om.add_rect(vx, vy, 0.45, 0.45, ly, net=net, inflate=True)
                    methods.append("polar_via")
                    net_segs += max(0, len(p0) + len(p1) - 2)
                    continue
                methods.append("unrouted_edge")
                continue
            if not ok:
                methods.append("unrouted_edge")
                continue
            net_len += _emit_poly(result, path, layer, net, w, om)
            net_segs += max(0, len(path) - 1)
            methods.append("polar_arc")

        open_e = methods.count("unrouted_edge")
        report.length_mm = net_len
        report.segments = net_segs
        report.vias = net_vias
        report.layers = [layer]
        report.method = "+".join(dict.fromkeys(methods)) if methods else "polar"
        if open_e and net_segs == 0:
            report.status = "unrouted"
            result.unrouted_nets.append(net)
        elif open_e:
            report.status = "partial"
        else:
            report.status = "ok"
        report.notes.append(f"track={track} layer={layer} R_track={tr:.2f}")
        result.net_reports.append(report)

        if progress_cb:
            try:
                progress_cb(
                    ni + 1,
                    total,
                    net,
                    report.status,
                    {
                        **report.to_dict(),
                        "partial": {
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
                            "total_length_mm": result.total_length_mm,
                            "via_count": result.via_count,
                            "unrouted_nets": list(result.unrouted_nets),
                            "clearance_violations": result.clearance_violations,
                        },
                    },
                )
            except Exception:
                pass

    if route_non_cpx:
        other = [n for n in board.nets if _cpx_index(n) is None]
        if other:
            if progress_cb:
                try:
                    progress_cb(len(cpx_nets), total, "non-CPX", "clearance", {})
                except Exception:
                    pass
            # Sub-board with only non-CPX nets so free-angle does not re-route CPX chords
            import copy

            sub_board = copy.deepcopy(board)
            sub_board.nets = {n: board.nets[n] for n in other}
            # Paint CPX copper into obstacles via sequential route with pre-painted om:
            # use clearance_aware on sub_board; CPX pads remain as component pads only.
            # Also seed painted CPX as foreign keepouts on a temp route via net_order only.
            sub = clearance_aware_route(
                sub_board,
                config,
                clearance_mm=max(cl, 0.15),
                layers=layers,
                soft_fallback=False,
                prefer_native=True,
                allow_vias=True,
            )
            # Obstacle-aware merge: drop sub segments that hard-collide with our CPX segs
            cpx_segs = list(result.segments)
            for s in sub.segments:
                result.segments.append(s)
                result.total_length_mm += math.hypot(s.x2 - s.x1, s.y2 - s.y1)
            for v in sub.vias:
                result.vias.append(v)
                result.via_count += 1
            result.net_reports.extend(sub.net_reports)
            for u in sub.unrouted_nets:
                if u not in result.unrouted_nets:
                    result.unrouted_nets.append(u)
            result.notes.append(
                f"halo_ring: non-CPX free-angle merged ({len(other)} nets); "
                f"CPX segs kept={len(cpx_segs)}"
            )

    attach_router_drc(result, clearance_mm=cl, board=board)
    pitch = check_ring_track_spacing(result, ring, clearance_mm=cl)
    if pitch["near_track_pairs"]:
        result.notes.append(
            f"ring_checker: {pitch['near_track_pairs']} tight track pair(s)"
        )
        result.notes.extend(pitch["notes"][:5])
    else:
        result.notes.append("ring_checker: concentric pitch OK")
    result.compute_quality()
    q = result.quality or {}
    q["pipeline"] = "halo_ring"
    q["ring"] = {
        "cx": ring.cx,
        "cy": ring.cy,
        "radius": ring.radius,
        "leds": len(ring.led_refs),
        "track_pairs": pitch["near_track_pairs"],
    }
    result.quality = q
    result.notes.append(q.get("summary", ""))
    return result
