"""Optional C++ core (`pr_native`). Falls back silently if not built.

Build: ``bash scripts/build_native.sh`` then add ``native/build`` to ``PYTHONPATH``.
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
        import pr_native  # type: ignore

        _native = pr_native
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
    return {
        "available": True,
        "version": m.version(),
        "gpu": dict(m.gpu_probe()),
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
) -> dict[str, Any] | None:
    """Run native router; return a dict compatible with ``RouteResult.to_dict()``."""
    m = _try_load()
    if m is None:
        return None

    from physics_router.router import board_extent

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
    cfg.max_expansions = 4000

    net_names = list(board.nets.keys())
    name_to_id = {n: i for i, n in enumerate(net_names)}

    nets = []
    for name in net_names:
        anchors = []
        seen: set[tuple[float, float]] = set()
        for ref, _pad in board.nets[name]:
            c = board.components.get(ref)
            if c is None:
                continue
            key = (round(c.x_mm, 3), round(c.y_mm, 3))
            if key in seen:
                continue
            seen.add(key)
            anchors.append(m.Vec2(c.x_mm, c.y_mm))
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
        else:
            ns.width_mm = 0.25
        ns.preferred_layers = list(range(cfg.num_layers))
        nets.append(ns)

    obstacles = []
    for _ref, c in board.components.items():
        nets_on = {p.get("net") for p in (c.pads or []) if p.get("net")}
        if len(nets_on) != 1:
            continue
        nid = name_to_id.get(next(iter(nets_on)), -1)
        ob = m.RectObs()
        ob.cx, ob.cy = c.x_mm, c.y_mm
        ob.w = max(min(c.width_mm, c.height_mm) * 0.35, 0.4)
        ob.h = ob.w
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
    vias = [
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
        }
        for v in result.vias
    ]
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
        "notes": list(result.notes) + [f"native={result.used_native}", f"gpu={result.used_gpu}"],
        "quality": {
            "score": result.quality_score,
            "grade": result.quality_grade,
            "summary": (
                f"grade {result.quality_grade} ({result.quality_score:.0f}/100) "
                f"native {result.elapsed_ms:.1f}ms"
            ),
        },
        "net_reports": net_reports,
        "segments": segments,
        "vias": vias,
        "elapsed_ms": result.elapsed_ms,
        "backend": "native",
    }
