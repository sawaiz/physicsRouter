"""Optional C++ core (`pr_native`). Falls back silently if not built.

Build: ``bash scripts/build_native.sh`` then add ``native/build`` to ``PYTHONPATH``.

Native v1.1: isotropic free-angle detours, multi-site vias with reasons,
post-rubberband, via minimize. Python remains the full TopoR pipeline host
(K-homotopy, CBS, elastic, SI/MFG); native accelerates the hot path.
"""

from __future__ import annotations

from typing import Any

_native = None
_load_error: str | None = None


def _try_load() -> Any:
    global _native, _load_error
    if _native is not None or _load_error is not None:
        return _native
    try:
        from physics_router.router import _native_core

        _native = _native_core()
        return _native
    except Exception as e:  # noqa: BLE001
        _load_error = str(e)
        return None


def available() -> bool:
    return _try_load() is not None


def info() -> dict[str, Any]:
    m = _try_load()
    if m is None:
        return {"available": False, "error": _load_error}
    ver = m.version() if hasattr(m, "version") else "unknown"
    return {
        "available": True,
        "version": ver,
        "gpu": dict(m.gpu_probe()),
        "features": {
            "isotropic": True,
            "post_rubberband": True,
            "via_minimize": True,
            "via_reasons": True,
        },
    }


def route_board_native(
    board: Any,
    config: Any,
    *,
    clearance_mm: float = 0.2,
    grid_mm: float = 0.5,
    soft_fallback: bool = False,
    allow_vias: bool = True,
    use_gpu: bool = True,
    isotropic: bool = True,
    post_rubberband: bool = True,
    via_minimize: bool = False,
    net_order: list[str] | None = None,
    exclusive_nets: bool = False,
    seed_segments: list[Any] | None = None,
    max_expansions: int | None = None,
) -> dict[str, Any] | None:
    """Run native router; return a dict compatible with ``RouteResult.to_dict()``.

    ``exclusive_nets=True`` with ``net_order`` routes **only** those nets (hybrid buckets).
    ``seed_segments``: prior copper painted as obstacles (net-aware keepouts).
    """
    m = _try_load()
    if m is None:
        return None

    from physics_router.router import board_extent, fanout_anchor

    x0, x1, y0, y1 = board_extent(board)
    layers = list(getattr(board, "copper_layers", None) or ["F.Cu", "B.Cu"])

    cfg = m.RouteConfig()
    cfg.x_min, cfg.x_max, cfg.y_min, cfg.y_max = x0, x1, y0, y1
    cfg.grid_mm = float(grid_mm)
    cfg.clearance_mm = float(clearance_mm)
    cfg.num_layers = max(1, len(layers))
    cfg.soft_fallback = bool(soft_fallback)
    cfg.allow_vias = bool(allow_vias)
    cfg.use_gpu = bool(use_gpu)
    # Scale A* budget with resolution; denser grids get more expansions
    n_nets = max(1, len(board.nets))
    base_exp = 5000 * max(1.0, 0.35 / max(float(grid_mm), 0.05))
    # Cap for speed on large multipin boards (e.g. HALO 23 nets × 90 LEDs)
    if max_expansions is not None:
        cfg.max_expansions = int(max(1000, max_expansions))
    else:
        cfg.max_expansions = int(
            min(12000 if n_nets > 16 else 20000, max(3000, base_exp))
        )
    # Exclusive multipin (1 net): allow higher budget for full connectivity
    if exclusive_nets and net_order and len(net_order) == 1 and max_expansions is None:
        name0 = net_order[0]
        n_pins = len(board.nets.get(name0) or [])
        if n_pins >= 8:
            cfg.max_expansions = int(max(cfg.max_expansions, min(32000, 4000 * n_pins)))
    if hasattr(cfg, "isotropic"):
        cfg.isotropic = bool(isotropic)
    if hasattr(cfg, "post_rubberband"):
        cfg.post_rubberband = bool(post_rubberband)
    if hasattr(cfg, "via_minimize"):
        cfg.via_minimize = bool(via_minimize)

    net_names = list(net_order) if net_order else list(board.nets.keys())
    if not exclusive_nets:
        # append any missing nets (full-board mode)
        for n in board.nets:
            if n not in net_names:
                net_names.append(n)
    else:
        net_names = [n for n in net_names if n in board.nets]
    name_to_id = {n: i for i, n in enumerate(net_names)}

    nets = []
    for name in net_names:
        if name not in board.nets:
            continue
        anchors = []
        seen: set[tuple[float, float]] = set()
        for ref, _pad in board.nets[name]:
            if ref not in board.components:
                continue
            # Real pad XY when available (matches Python path)
            ax, ay = fanout_anchor(board, ref, name, pad_num=str(_pad))
            key = (round(ax, 3), round(ay, 3))
            if key in seen:
                continue
            seen.add(key)
            anchors.append(m.Vec2(ax, ay))
        ns = m.NetSpec()
        ns.net_id = name_to_id[name]
        ns.name = name
        ns.anchors = anchors
        ns.priority = float(config.weight_for_net(name)) if config else 1.0
        lab = config.net_by_name().get(name) if config else None
        if lab is not None:
            nc = lab.net_class.value if hasattr(lab.net_class, "value") else str(lab.net_class)
            if nc in ("power", "ground"):
                ns.width_mm = 0.5
            elif nc in ("high_speed", "differential", "rf"):
                ns.width_mm = 0.2
            else:
                ns.width_mm = 0.25
            if lab.critical:
                ns.priority *= 1.5
        else:
            ns.width_mm = 0.25
        # Stripe multipin matrix / CPX nets across layers to reduce same-layer crossings
        is_matrix = name.upper().startswith("CPX") or len(anchors) >= 12
        if is_matrix and cfg.num_layers >= 2:
            try:
                idx = int("".join(ch for ch in name if ch.isdigit()) or "0")
            except ValueError:
                idx = abs(hash(name)) % cfg.num_layers
            primary = idx % cfg.num_layers
            ns.preferred_layers = [primary] + [
                i for i in range(cfg.num_layers) if i != primary
            ]
            # Slight priority demotion for dense buses so sparse nets paint first
            ns.priority *= 0.85
        else:
            ns.preferred_layers = list(range(cfg.num_layers))
        nets.append(ns)

    # Sort nets for native by priority (already handled in C++ too)
    nets.sort(key=lambda n: (-n.priority, len(n.anchors), n.name))

    obstacles = []
    for _ref, c in board.components.items():
        nets_on = {p.get("net") for p in (c.pads or []) if p.get("net")}
        # Single-net parts: keepout owned by that net (same-net may pass)
        # Multi-net ICs: hard keepout (net_id=-1) so free-angle cannot cross the package
        if len(nets_on) == 1:
            nid = name_to_id.get(next(iter(nets_on)), -1)
        elif len(nets_on) > 1:
            nid = -1
        else:
            continue
        ob = m.RectObs()
        ob.cx, ob.cy = c.x_mm, c.y_mm
        # Slightly larger body keepout for multi-net packages
        scale = 0.45 if nid < 0 else 0.35
        ob.w = max(min(c.width_mm, c.height_mm) * scale, 0.4)
        ob.h = ob.w
        ob.net_id = nid
        obstacles.append(ob)

    # Seed prior hybrid-phase copper as net-aware keepouts (approx. along segments)
    if seed_segments:
        import math

        layer_to_id = {ly: i for i, ly in enumerate(layers)}
        for s in seed_segments:
            nid = name_to_id.get(getattr(s, "net", ""), -1)
            # Unknown nets block everyone
            w = float(getattr(s, "width_mm", 0.25) or 0.25) + float(clearance_mm)
            x1, y1 = float(s.x1), float(s.y1)
            x2, y2 = float(s.x2), float(s.y2)
            length = math.hypot(x2 - x1, y2 - y1)
            steps = max(1, int(length / max(float(grid_mm), 0.1)))
            for i in range(steps + 1):
                t = i / steps
                ob = m.RectObs()
                ob.cx = x1 + (x2 - x1) * t
                ob.cy = y1 + (y2 - y1) * t
                ob.w = w
                ob.h = w
                ob.net_id = nid
                obstacles.append(ob)

    result = m.route_board(nets, cfg, obstacles)
    id_to_name = {i: n for n, i in name_to_id.items()}

    segments = [
        {
            "net": id_to_name.get(s.net_id, str(s.net_id)),
            "x1": s.x1,
            "y1": s.y1,
            "x2": s.x2,
            "y2": s.y2,
            "layer": layers[s.layer] if 0 <= s.layer < len(layers) else layers[0],
            "width_mm": s.width_mm,
        }
        for s in result.segments
    ]
    vias = []
    for v in result.vias:
        reason = getattr(v, "reason", "") or ""
        alts = int(getattr(v, "alternatives_considered", 0) or 0)
        vias.append(
            {
                "net": id_to_name.get(v.net_id, str(v.net_id)),
                "x": v.x,
                "y": v.y,
                "size_mm": v.size_mm,
                "drill_mm": v.size_mm * 0.5,
                "layers": [
                    layers[v.layer_a] if 0 <= v.layer_a < len(layers) else layers[0],
                    layers[v.layer_b] if 0 <= v.layer_b < len(layers) else layers[-1],
                ],
                "reason": reason,
                "alternatives_considered": alts,
                "blocked_same_layer": [],
            }
        )
    net_reports = [
        {
            "net": nr.name,
            "pins": nr.pins,
            "length_mm": nr.length_mm,
            "segments": nr.segments,
            "vias": nr.vias,
            "status": nr.status,
            "method": nr.method,
            "notes": [],
        }
        for nr in result.net_reports
    ]

    return {
        "total_length_mm": result.total_length_mm,
        "via_count": result.via_count,
        "unrouted_nets": list(result.unrouted),
        "clearance_violations": result.clearance_violations,
        "notes": list(result.notes)
        + [f"native={result.used_native}", f"gpu={result.used_gpu}"],
        "quality": {
            "score": result.quality_score,
            "grade": result.quality_grade,
            "summary": (
                f"grade {result.quality_grade} ({result.quality_score:.0f}/100) "
                f"native {result.elapsed_ms:.1f}ms"
            ),
            "pipeline": "native_isotropic",
            "explanations": {
                "vias": [
                    {
                        "net": v["net"],
                        "x": v["x"],
                        "y": v["y"],
                        "layers": v["layers"],
                        "reason": v.get("reason") or "",
                        "alternatives_considered": v.get("alternatives_considered", 0),
                    }
                    for v in vias
                    if v.get("reason")
                ],
                "summary": f"{len(vias)} via(s) from native isotropic router",
            },
        },
        "net_reports": net_reports,
        "segments": segments,
        "vias": vias,
        "elapsed_ms": result.elapsed_ms,
        "backend": "native",
    }


def polish_native_with_python(
    board: Any,
    config: Any,
    raw: dict[str, Any],
    *,
    clearance_mm: float = 0.2,
    via_minimize: bool = False,
) -> Any:
    """Apply Python elastic + SI/MFG + via explain polish to a native route dict."""
    from physics_router.router import _route_result_from_dict, rubberband_cleanup, remove_redundant_vias
    from physics_router.elastic import elastic_optimize_route
    from physics_router.si_mfg import evaluate_si_mfg

    r = _route_result_from_dict(raw)
    r = rubberband_cleanup(r, board, config, clearance_mm=clearance_mm)
    r = remove_redundant_vias(
        r, board, config, clearance_mm=clearance_mm, aggressive=via_minimize
    )
    if len(board.nets) <= 40:
        r = elastic_optimize_route(r, board, clearance_mm=clearance_mm, iterations=12)
    si = evaluate_si_mfg(r, board, config, clearance_mm=clearance_mm)
    r.quality = {
        **(r.quality or {}),
        "si_mfg": si.to_dict(),
        "backend": "native+python_polish",
        "explanations": (raw.get("quality") or {}).get("explanations")
        or {
            "vias": [
                {
                    "net": v.net,
                    "x": v.x,
                    "y": v.y,
                    "layers": list(v.layers),
                    "reason": v.reason,
                    "alternatives_considered": v.alternatives_considered,
                }
                for v in r.vias
            ],
        },
    }
    for n in si.notes:
        r.notes.append(n)
    r.notes.append(
        "polish: rubberband + "
        + ("via_minimize" if via_minimize else "keep-vias")
        + " + elastic + SI/MFG"
    )
    r.compute_quality()
    return r
