"""Required C++ core (``pr_native``) bridge.

Build: ``bash scripts/build_native.sh`` then add ``native/build`` to ``PYTHONPATH``.

Native v1.9: graph-planned atomic layer-reachable routing, oriented pad
obstacles, topology-safe multipin polish, organic copper areas, and C++
historical-cost geometry for board-wide negotiated congestion.
Python remains the TopoR policy/file-format host; C++ owns geometry.
"""

from __future__ import annotations

import math
import os
from typing import Any

_native = None
_load_error: str | None = None


def _host_parallelism() -> dict[str, Any]:
    """Detect CPU cores and rough RAM for expansion / thread budgets."""
    cores = os.cpu_count() or 4
    # Prefer physical-ish bound for OpenMP when oversubscribed hyperthreads
    ram_gb = 8.0
    try:
        import psutil  # type: ignore

        ram_gb = float(psutil.virtual_memory().total) / (1024**3)
    except Exception:
        pass
    if ram_gb <= 8.5:
        # Linux
        try:
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        ram_gb = float(line.split()[1]) / (1024 * 1024)
                        break
        except Exception:
            pass
    if ram_gb <= 8.5:
        # macOS
        try:
            import subprocess

            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True
            ).strip()
            ram_gb = float(out) / (1024**3)
        except Exception:
            pass
    if ram_gb <= 8.5:
        # Windows
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                ram_gb = float(stat.ullTotalPhys) / (1024**3)
        except Exception:
            pass
    return {"cores": int(cores), "ram_gb": float(ram_gb)}


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
    host = _host_parallelism()
    return {
        "available": True,
        "version": ver,
        "gpu": dict(m.gpu_probe()),
        "host": host,
        "features": {
            "isotropic": True,
            "post_rubberband": True,
            "via_minimize": True,
            "via_reasons": True,
            "atomic_nets": True,
            "copper_areas": True,
            "topology_safe_rubberband": True,
            "matrix_bundle": True,
            "oriented_pad_obstacles": True,
            "anchor_layer_reachability": True,
            "width_aware_obstacle_inflation": True,
            "hypergraph_topology": True,
            "crossing_aware_mst": True,
            "dsatur_layer_coloring": True,
            "pathfinder_history": True,
            "conflict_directed_ripup": True,
            "no_via_in_pad": True,
            "pin_access_oracle": True,
            "section_layer_planning": True,
            "openmp_threads": host["cores"],
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
    use_copper_areas: bool = False,
    congestion: Any | None = None,
    routing_plan: Any | None = None,
) -> dict[str, Any] | None:
    """Run native router; return a dict compatible with ``RouteResult.to_dict()``.

    ``exclusive_nets=True`` with ``net_order`` routes **only** those nets (hybrid buckets).
    ``seed_segments``: prior copper painted as obstacles (net-aware keepouts).
    ``congestion``: sparse present/historical PathFinder costs.
    """
    m = _try_load()
    if m is None:
        return None

    from physics_router.router import (
        board_extent,
        fanout_anchor,
        outline_polygon_from_board,
    )

    x0, x1, y0, y1 = board_extent(board)
    layers = list(getattr(board, "copper_layers", None) or ["F.Cu", "B.Cu"])

    cfg = m.RouteConfig()
    cfg.x_min, cfg.x_max, cfg.y_min, cfg.y_max = x0, x1, y0, y1
    outline = outline_polygon_from_board(board)
    if outline:
        cfg.board_outline = [m.Vec2(x, y) for x, y in outline]
    cfg.grid_mm = float(grid_mm)
    cfg.clearance_mm = float(clearance_mm)
    # C++ capacity-mesh global planning (runs inside route_board)
    if hasattr(cfg, "enable_capacity_mesh"):
        cfg.enable_capacity_mesh = True
    if hasattr(cfg, "capacity_effort"):
        effort = 0.55
        if routing_plan is not None:
            effort = float(
                (getattr(routing_plan, "metrics", None) or {}).get("effort", 0.55)
            )
        cfg.capacity_effort = effort
    rules = dict(getattr(board, "design_rules", None) or {})
    if hasattr(cfg, "edge_clearance_mm"):
        cfg.edge_clearance_mm = float(
            rules.get("min_copper_edge_clearance_mm") or 0.01
        )
    if hasattr(cfg, "via_diameter_mm"):
        planned_diameter = getattr(
            getattr(routing_plan, "pin_access", None), "via_diameter_mm", None
        )
        cfg.via_diameter_mm = float(
            rules.get("min_via_diameter_mm") or planned_diameter or 0.8
        )
    if hasattr(cfg, "via_drill_mm"):
        planned_drill = getattr(
            getattr(routing_plan, "pin_access", None), "via_drill_mm", None
        )
        cfg.via_drill_mm = float(
            rules.get("min_via_drill_mm") or planned_drill or 0.4
        )
    if hasattr(cfg, "min_hole_to_hole_mm"):
        cfg.min_hole_to_hole_mm = float(
            rules.get("min_hole_to_hole_mm") or 0.25
        )
    if hasattr(cfg, "allow_blind_buried_vias"):
        cfg.allow_blind_buried_vias = bool(
            rules.get("allow_blind_buried_vias", False)
        )
    cfg.num_layers = max(1, len(layers))
    cfg.soft_fallback = bool(soft_fallback)
    cfg.allow_vias = bool(allow_vias)
    cfg.use_gpu = bool(use_gpu)
    host = _host_parallelism()
    if hasattr(cfg, "threads"):
        # Drive OpenMP to all logical cores (oversubscribe slightly on HT)
        cfg.threads = int(host["cores"])
        os.environ.setdefault("OMP_NUM_THREADS", str(cfg.threads))
        os.environ.setdefault("OMP_PROC_BIND", "close")
    # Scale A* budget with resolution + RAM headroom
    n_nets = max(1, len(board.nets))
    base_exp = 5000 * max(1.0, 0.35 / max(float(grid_mm), 0.05))
    ram_scale = 1.0
    if host["ram_gb"] >= 24:
        ram_scale = 2.0
    elif host["ram_gb"] >= 12:
        ram_scale = 1.5
    exp_cap = int((24000 if n_nets > 16 else 40000) * ram_scale)
    if max_expansions is not None:
        cfg.max_expansions = int(max(1000, max_expansions))
    else:
        cfg.max_expansions = int(
            min(exp_cap, max(3000, base_exp * ram_scale))
        )
    # Exclusive multipin (1 net): allow higher budget for full connectivity
    if exclusive_nets and net_order and len(net_order) == 1 and max_expansions is None:
        name0 = net_order[0]
        n_pins = len(board.nets.get(name0) or [])
        if n_pins >= 8:
            cfg.max_expansions = int(
                max(cfg.max_expansions, min(int(48000 * ram_scale), 4000 * n_pins))
            )
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
    layer_to_id = {layer: index for index, layer in enumerate(layers)}
    if congestion is not None and hasattr(cfg, "congestion"):
        cfg.congestion_cell_mm = float(
            getattr(congestion, "cell_mm", 0.5) or 0.5
        )
        present_weight = float(getattr(congestion, "present_weight", 1.0))
        historical_weight = float(
            getattr(congestion, "historical_weight", 1.0)
        )
        combined: dict[tuple[int, int, str], float] = {}
        for key, value in getattr(congestion, "present", {}).items():
            combined[key] = combined.get(key, 0.0) + present_weight * float(value)
        for key, value in getattr(congestion, "historical", {}).items():
            combined[key] = combined.get(key, 0.0) + historical_weight * float(value)
        cells = []
        for (ix, iy, layer), cost in sorted(combined.items()):
            if layer not in layer_to_id or cost <= 0.0:
                continue
            cell = m.CongestionCell()
            cell.ix = int(ix)
            cell.iy = int(iy)
            cell.layer = layer_to_id[layer]
            cell.cost = float(cost)
            cells.append(cell)
        cfg.congestion = cells

    graph_plan = None
    graph_plan_error = ""
    try:
        from physics_router.graph_theory import plan_graph_topology

        # A one-net atomic retry still needs the board-level conflict coloring;
        # coloring a singleton always returns F.Cu and destroys the layer plan
        # that motivated the retry. Topology edges are consumed only for the
        # selected net, while assignment sees all competing nets.
        planning_names = (
            list(board.nets)
            if exclusive_nets and len(net_names) == 1
            else net_names
        )
        graph_plan = plan_graph_topology(
            board,
            config,
            net_names=planning_names,
            layers=layers,
        )
    except Exception as exc:  # noqa: BLE001
        # Topology is advisory: geometry remains available if incomplete board
        # metadata makes planning impossible.
        graph_plan_error = str(exc)
    topology_plan = routing_plan if routing_plan is not None else graph_plan

    nets = []
    default_class = dict((rules.get("net_classes") or {}).get("Default") or {})
    board_track_width = max(
        float(rules.get("min_track_width_mm") or 0.15),
        float(default_class.get("track_width_mm") or 0.15),
    )
    for name in net_names:
        if name not in board.nets:
            continue
        anchors = []
        anchor_layers: list[set[int]] = []
        seen: dict[tuple[float, float], int] = {}
        for ref, _pad in board.nets[name]:
            if ref not in board.components:
                continue
            # Real pad XY when available (matches Python path)
            ax, ay = fanout_anchor(board, ref, name, pad_num=str(_pad))
            key = (round(ax, 3), round(ay, 3))
            component = board.components[ref]
            pad_data = next(
                (
                    value
                    for value in component.pads or []
                    if str(value.get("num")) == str(_pad)
                ),
                {},
            )
            raw_layers = list(pad_data.get("layers") or [])
            if "*.Cu" in raw_layers:
                allowed = set(range(cfg.num_layers))
            else:
                allowed = {
                    layer_to_id[layer] for layer in raw_layers if layer in layer_to_id
                }
            if not allowed:
                # Missing pad metadata is kept compatible but conservative for
                # the common front-SMD case.
                allowed = {0}
            if key in seen:
                anchor_layers[seen[key]].update(allowed)
                continue
            seen[key] = len(anchors)
            anchors.append(m.Vec2(ax, ay))
            anchor_layers.append(set(allowed))
        ns = m.NetSpec()
        ns.net_id = name_to_id[name]
        ns.name = name
        ns.anchors = anchors
        ns.anchor_layers = [sorted(value) for value in anchor_layers]
        if topology_plan is not None and hasattr(ns, "topology_edges"):
            ns.topology_edges = topology_plan.topology_edges(name)
        if routing_plan is not None and hasattr(ns, "topology_edge_layers"):
            ns.topology_edge_layers = [
                layer_to_id[layer]
                for layer in routing_plan.topology_edge_layers(name)
                if layer in layer_to_id
            ]
        if routing_plan is not None and hasattr(ns, "anchor_via_sites"):
            ns.anchor_via_sites = [
                [m.Vec2(x, y) for x, y in routing_plan.access_sites_for(name, index)]
                for index in range(len(anchors))
            ]
        # An exclusive bucket's caller order is the routing policy (and may be
        # a deliberate rebuild variant). Full-board calls still use semantic
        # net weights. Keeping bucket priorities equal lets the C++ stable sort
        # honor the exact supplied order.
        ns.priority = (
            1.0
            if exclusive_nets and net_order is not None
            else float(config.weight_for_net(name)) if config else 1.0
        )
        lab = config.net_by_name().get(name) if config else None
        if lab is not None:
            nc = (
                lab.net_class.value
                if hasattr(lab.net_class, "value")
                else str(lab.net_class)
            )
            if nc in ("power", "ground"):
                ns.width_mm = max(board_track_width, 0.3)
            elif nc in ("high_speed", "differential", "rf"):
                ns.width_mm = board_track_width
            else:
                ns.width_mm = board_track_width
            if lab.critical and not (exclusive_nets and net_order is not None):
                ns.priority *= 1.5
        else:
            ns.width_mm = board_track_width
            nc = ""
        planned_layers = (
            routing_plan.preferred_layers(name) if routing_plan is not None else []
        )
        graph_layer = (
            graph_plan.layer_assignment.get(name) if graph_plan is not None else None
        )
        # Graph coloring replaces name-based striping: nets whose straight-line
        # trees cross are assigned distinct preferred layers when possible.
        is_matrix = name.upper().startswith("CPX") or len(anchors) >= 12
        fallback_primary = 0
        if is_matrix and cfg.num_layers >= 2:
            digits = "".join(ch for ch in name if ch.isdigit())
            fallback_primary = (
                int(digits) if digits else sum(ord(ch) for ch in name)
            ) % cfg.num_layers
        graph_primary = layer_to_id.get(
            planned_layers[0] if planned_layers else graph_layer,
            fallback_primary,
        )
        if is_matrix and cfg.num_layers >= 2:
            ns.preferred_layers = [graph_primary] + [
                i for i in range(cfg.num_layers) if i != graph_primary
            ]
            # Slight priority demotion for dense buses so sparse nets paint first
            ns.priority *= 0.85
        else:
            ns.preferred_layers = [graph_primary] + [
                i for i in range(cfg.num_layers) if i != graph_primary
            ]
        nu = name.upper()
        is_power_area = (
            nc in ("power", "ground")
            or nu in (
                "GND",
                "AGND",
                "DGND",
                "VSS",
                "PGND",
                "VCC",
                "VDD",
                "VBAT",
                "HV",
                "+3V",
                "+5V",
                "+3V3",
                "+1V2",
                "+1V8",
                "+5V-A",
                "-5V",
            )
            or nu.startswith("+")
            or nu.startswith("-")
            or "GND" in nu
        )
        if use_copper_areas and is_power_area and len(anchors) >= 2:
            ns.use_copper_area = True
            # Prefer F.Cu (layer 0) so SMD pads are inside the pour connectivity
            # test; inner-only pours left outer SMD power nets incomplete.
            ns.area_layer = 0
            # Larger margin → bigger organic hull (helps multipin GND completeness)
            ns.area_margin_mm = max(1.6, float(clearance_mm) * 6.0)
            ns.area_priority = 10 if nc == "ground" or "GND" in nu else 20
        nets.append(ns)

    # Exclusive bucket calls carry an exact policy order, including rebuild
    # variants. Full-board calls retain semantic priority/few-pin ordering.
    if not (exclusive_nets and net_order is not None):
        nets.sort(key=lambda n: (-n.priority, len(n.anchors)))

    from physics_router.kicad_io import local_to_board

    obstacles = []
    for _ref, component in board.components.items():
        # Pads are copper obstacles owned by their net. Package bodies are not
        # net-agnostic copper keepouts: painting a multi-pin IC body blocked the
        # pad anchors inside it and made every U1 signal impossible to start.
        for pad in component.pads or []:
            pad_net = str(pad.get("net") or "")
            x, y = local_to_board(
                component.x_mm,
                component.y_mm,
                component.rotation_deg,
                float(pad.get("x") or 0.0),
                float(pad.get("y") or 0.0),
            )
            pad_layers = list(pad.get("layers") or [])
            if "*.Cu" in pad_layers:
                copper_layer_ids = list(range(cfg.num_layers))
            else:
                copper_layer_ids = [
                    layer_to_id[layer] for layer in pad_layers if layer in layer_to_id
                ]
            if not copper_layer_ids:
                continue
            ob = m.RectObs()
            ob.cx, ob.cy = x, y
            # KiCad's parsed pad rotation is already the board-space angle;
            # preserve it in the native oriented obstacle. An AABB joins the
            # clearance envelopes of diagonal fine-pitch pads into a false
            # wall even though their outward fanout corridors are legal.
            ob.w = max(float(pad.get("w") or 0.0), 0.2)
            ob.h = max(float(pad.get("h") or 0.0), 0.2)
            if hasattr(ob, "rotation_deg"):
                ob.rotation_deg = -float(pad.get("rot") or 0.0)
            # Unassigned copper is still copper and blocks every routed net.
            ob.net_id = name_to_id.get(pad_net, -1)
            ob.layers = copper_layer_ids
            if hasattr(ob, "is_pad"):
                ob.is_pad = True
            obstacles.append(ob)

            # KiCad custom pads may carry copper far outside their anchor
            # ``size`` (HALO's coin-cell contacts are wide stroked arcs). Add
            # each sampled primitive segment as an oriented copper obstacle.
            pad_angle = math.radians(-float(pad.get("rot") or 0.0))
            cosine, sine = math.cos(pad_angle), math.sin(pad_angle)
            for stroke in pad.get("custom_strokes") or []:
                points = list(stroke.get("pts") or [])
                stroke_width = abs(float(stroke.get("width") or 0.0))
                if len(points) < 2 or stroke_width <= 0.0:
                    continue
                transformed = [
                    (
                        x + float(point[0]) * cosine - float(point[1]) * sine,
                        y + float(point[0]) * sine + float(point[1]) * cosine,
                    )
                    for point in points
                ]
                for start, end in zip(transformed, transformed[1:]):
                    length = math.hypot(end[0] - start[0], end[1] - start[1])
                    primitive = m.RectObs()
                    primitive.cx = 0.5 * (start[0] + end[0])
                    primitive.cy = 0.5 * (start[1] + end[1])
                    primitive.w = length + stroke_width
                    primitive.h = stroke_width
                    if hasattr(primitive, "rotation_deg"):
                        primitive.rotation_deg = math.degrees(
                            math.atan2(end[1] - start[1], end[0] - start[0])
                        )
                    primitive.net_id = ob.net_id
                    primitive.layers = copper_layer_ids
                    if hasattr(primitive, "is_pad"):
                        primitive.is_pad = True
                    obstacles.append(primitive)

    # Seed prior hybrid-phase copper as net-aware keepouts (approx. along segments)
    if seed_segments:
        for s in seed_segments:
            nid = name_to_id.get(getattr(s, "net", ""), -1)
            # Seed samples describe physical copper width. The native grid adds
            # the new track half-width plus clearance exactly once when it
            # paints every RectObs; pre-inflating here double-counted clearance
            # and starved later hybrid phases.
            w = float(getattr(s, "width_mm", 0.25) or 0.25)
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
                ob.layers = [layer_to_id.get(getattr(s, "layer", ""), 0)]
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
                "drill_mm": getattr(v, "drill_mm", v.size_mm * 0.5),
                "layers": [
                    layers[v.layer_a] if 0 <= v.layer_a < len(layers) else layers[0],
                    layers[v.layer_b] if 0 <= v.layer_b < len(layers) else layers[-1],
                ],
                "reason": reason,
                "alternatives_considered": alts,
                "blocked_same_layer": [],
            }
        )
    areas = [
        {
            "net": id_to_name.get(area.net_id, str(area.net_id)),
            "layer": layers[area.layer] if 0 <= area.layer < len(layers) else layers[0],
            "outline": [[point.x, point.y] for point in area.outline],
            "clearance_mm": area.clearance_mm,
            "min_thickness_mm": area.min_thickness_mm,
            "priority": area.priority,
        }
        for area in getattr(result, "areas", [])
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
        "notes": list(result.notes)
        + [
            f"native={result.used_native}",
            f"gpu={result.used_gpu}",
            (
                "routing_plan=" + str(routing_plan.to_dict())
                if routing_plan is not None
                else "graph_topology=" + str(graph_plan.to_dict())
                if graph_plan is not None
                else "graph_topology=fallback " + graph_plan_error
            ),
        ],
        "quality": {
            "score": result.quality_score,
            "grade": result.quality_grade,
            "summary": (
                f"grade {result.quality_grade} ({result.quality_score:.0f}/100) "
                f"native {result.elapsed_ms:.1f}ms"
            ),
            "pipeline": "native_isotropic",
            "graph_topology_plan": (
                graph_plan.to_dict() if graph_plan is not None else None
            ),
            "production_route_plan": (
                routing_plan.to_dict() if routing_plan is not None else None
            ),
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
        "areas": areas,
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
    from physics_router.router import (
        _route_result_from_dict,
        rubberband_cleanup,
        remove_redundant_vias,
    )
    from physics_router.elastic import elastic_optimize_route
    from physics_router.si_mfg import evaluate_si_mfg

    r = _route_result_from_dict(raw)
    r = rubberband_cleanup(r, board, config, clearance_mm=clearance_mm)
    r = remove_redundant_vias(
        r, board, config, clearance_mm=clearance_mm, aggressive=via_minimize
    )
    if len(board.nets) <= 40:
        r = elastic_optimize_route(
            r, board, clearance_mm=clearance_mm, iterations=12, config=config
        )
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
