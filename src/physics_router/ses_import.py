"""Import FreeRouting / Specctra SES wiring into RouteResult or KiCad PCB.

Parses a practical subset of Specctra session files:

* ``(wire (path LAYER WIDTH x y …) (net "NAME"))``
* FreeRouting ``(routes (network_out (net "N" (wire …) (via …))))``
* ``(via NAME x y …)`` padstack-style vias
* Resolution unit (mil / mm / inch)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from physics_router.kicad_io import parse_sexpr
from physics_router.router import RouteResult, RouteSegment, Via, append_routes_to_kicad_pcb


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _find_all(node: Any, head: str) -> list[list[Any]]:
    out: list[list[Any]] = []
    if isinstance(node, list) and node:
        if str(node[0]) == head:
            out.append(node)
        for child in node[1:]:
            out.extend(_find_all(child, head))
    return out


def _find_first(node: Any, head: str) -> list[Any] | None:
    if isinstance(node, list) and node:
        if str(node[0]) == head:
            return node
        for child in node[1:]:
            hit = _find_first(child, head)
            if hit is not None:
                return hit
    return None


def _detect_unit_scale(root: Any) -> float:
    """Return multiplier from file units → millimetres."""
    res = _find_first(root, "resolution")
    if res and len(res) >= 2:
        unit = str(res[1]).lower()
        if unit in ("mil", "mils"):
            return 0.0254
        if unit in ("inch", "in"):
            return 25.4
        if unit in ("mm", "millimeter", "millimetre"):
            return 1.0
        if unit in ("um", "micron", "µm"):
            return 0.001
    for unit_node in _find_all(root, "unit"):
        if len(unit_node) >= 2:
            unit = str(unit_node[1]).lower()
            if unit in ("mil", "mils"):
                return 0.0254
            if unit in ("inch", "in"):
                return 25.4
            if unit in ("mm",):
                return 1.0
    return 0.0254  # FreeRouting default


def _layer_from_token(tok: str, copper: list[str]) -> str:
    s = str(tok).strip('"')
    m = re.match(r"Signal[_\s-]?(\d+)", s, re.I)
    if m:
        i = int(m.group(1))
        if 0 <= i < len(copper):
            return copper[i]
    aliases = {
        "front": "F.Cu",
        "top": "F.Cu",
        "f.cu": "F.Cu",
        "back": "B.Cu",
        "bottom": "B.Cu",
        "b.cu": "B.Cu",
    }
    low = s.lower()
    if low in aliases and aliases[low] in copper:
        return aliases[low]
    if s in copper:
        return s
    for ly in copper:
        if ly.lower() == low:
            return ly
    return copper[0] if copper else "F.Cu"


def _net_from_node(node: list[Any], fallback: str = "") -> str:
    nn = _find_first(node, "net")
    if nn and len(nn) >= 2:
        return str(nn[1]).strip('"')
    return fallback


def _coords_to_points(
    coords: list[Any], scale: float, ox: float, oy: float
) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    i = 0
    while i < len(coords):
        c = coords[i]
        if isinstance(c, list):
            if c and str(c[0]) in ("xy", "xyz") and len(c) >= 3:
                pts.append((scale * _as_float(c[1]) + ox, scale * _as_float(c[2]) + oy))
            i += 1
            continue
        if i + 1 < len(coords) and not isinstance(coords[i + 1], list):
            pts.append(
                (scale * _as_float(coords[i]) + ox, scale * _as_float(coords[i + 1]) + oy)
            )
            i += 2
        else:
            i += 1
    return pts


def _append_path_segments(
    segs: list[RouteSegment],
    path: list[Any],
    *,
    net: str,
    copper: list[str],
    scale: float,
    ox: float,
    oy: float,
) -> None:
    if not path or len(path) < 5:
        return
    layer = _layer_from_token(str(path[1]), copper)
    if scale == 0.0254:
        width = max(0.0254 * _as_float(path[2], 10), 0.05)
    else:
        width = max(scale * _as_float(path[2], 0.25 / scale if scale else 0.25), 0.05)
    pts = _coords_to_points(path[3:], scale, ox, oy)
    for a, b in zip(pts, pts[1:]):
        if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9:
            continue
        segs.append(
            RouteSegment(
                x1=a[0],
                y1=a[1],
                x2=b[0],
                y2=b[1],
                layer=layer,
                net=net or "NET",
                width_mm=width,
            )
        )


def _named_net_nodes(root: Any) -> list[list[Any]]:
    """FreeRouting ``(net "NAME" (wire…) (via…))`` wrappers under network_out.

    Ignores bare ``(net "NAME")`` tags that only label a wire — those belong to
    the flat parse path.
    """
    out: list[list[Any]] = []
    for net_node in _find_all(root, "net"):
        if len(net_node) < 3 or isinstance(net_node[1], list):
            continue
        name = str(net_node[1]).strip('"')
        if not name:
            continue
        # Must contain route children, not just the name token
        has_route = any(
            isinstance(c, list) and c and str(c[0]) in ("wire", "wire_fromto", "via")
            for c in net_node[2:]
        )
        if has_route:
            out.append(net_node)
    return out


def _path_from_wire(wire: list[Any]) -> list[Any] | None:
    path = _find_first(wire, "path")
    if path is not None:
        return path
    if len(wire) >= 6 and not isinstance(wire[1], list):
        return ["path", wire[1], wire[2], *wire[3:]]
    return None


def parse_ses_to_route(
    ses_path: str | Path,
    *,
    copper_layers: list[str] | None = None,
    origin_mm: tuple[float, float] = (0.0, 0.0),
    unit_scale_mm: float | None = None,
) -> RouteResult:
    """Parse SES wiring into a :class:`RouteResult`."""
    copper = list(copper_layers or ["F.Cu", "B.Cu"])
    text = Path(ses_path).read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    root = parse_sexpr(text)
    scale = float(unit_scale_mm) if unit_scale_mm is not None else _detect_unit_scale(root)
    ox, oy = origin_mm
    segs: list[RouteSegment] = []
    vias: list[Via] = []

    named = _named_net_nodes(root)
    if named:
        # Prefer nested FreeRouting form — avoids double-count with flat scan
        for net_node in named:
            net_name = str(net_node[1]).strip('"')
            for wire in _find_all(net_node, "wire") + _find_all(net_node, "wire_fromto"):
                path = _path_from_wire(wire)
                if path is None:
                    continue
                _append_path_segments(
                    segs, path, net=net_name, copper=copper, scale=scale, ox=ox, oy=oy
                )
            for via in _find_all(net_node, "via"):
                _parse_via(via, vias, copper, scale, ox, oy, default_net=net_name)
    else:
        # Flat wires/vias with optional (net …) on the wire itself
        for wire in _find_all(root, "wire") + _find_all(root, "wire_fromto"):
            net = _net_from_node(wire) or "NET"
            path = _path_from_wire(wire)
            if path is None:
                continue
            _append_path_segments(
                segs, path, net=net, copper=copper, scale=scale, ox=ox, oy=oy
            )
        for via in _find_all(root, "via"):
            _parse_via(via, vias, copper, scale, ox, oy, default_net="")

    segs = _dedup_segments(segs)
    vias = _dedup_vias(vias)

    result = RouteResult(segments=segs, vias=vias, via_count=len(vias))
    result.total_length_mm = sum(
        ((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) ** 0.5 for s in segs
    )
    result.notes.append(
        f"imported SES: {len(segs)} segs, {len(vias)} vias from {Path(ses_path).name} "
        f"(scale={scale} mm/unit)"
    )
    result.compute_quality()
    return result


def _dedup_segments(segs: list[RouteSegment]) -> list[RouteSegment]:
    seen: set[tuple] = set()
    uniq: list[RouteSegment] = []
    # Prefer named nets over placeholder "NET"
    ordered = sorted(segs, key=lambda s: (0 if s.net and s.net != "NET" else 1))
    for s in ordered:
        # Normalize direction for dedup
        a = (round(s.x1, 4), round(s.y1, 4))
        b = (round(s.x2, 4), round(s.y2, 4))
        if a > b:
            a, b = b, a
        key = (a, b, s.layer)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    return uniq


def _dedup_vias(vias: list[Via]) -> list[Via]:
    seen: set[tuple] = set()
    uniq: list[Via] = []
    ordered = sorted(vias, key=lambda v: (0 if v.net and v.net != "NET" else 1))
    for v in ordered:
        key = (round(v.x, 4), round(v.y, 4))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(v)
    return uniq


def _parse_via(
    via: list[Any],
    vias: list[Via],
    copper: list[str],
    scale: float,
    ox: float,
    oy: float,
    *,
    default_net: str,
) -> None:
    """Parse (via "Padstack" x y …) or (via x y …)."""
    if len(via) < 3:
        return
    nums: list[float] = []
    for item in via[1:]:
        if isinstance(item, list):
            continue
        try:
            nums.append(float(item))
        except (TypeError, ValueError):
            continue
        if len(nums) >= 2:
            break
    if len(nums) < 2:
        return
    x = scale * nums[0] + ox
    y = scale * nums[1] + oy
    net = _net_from_node(via, default_net) or default_net or "NET"
    size_mm, drill_mm = 0.6, 0.3
    if len(via) >= 2 and isinstance(via[1], str) and not via[1].replace(".", "", 1).isdigit():
        m = re.search(r"(\d+):(\d+)", str(via[1]))
        if m:
            size_mm = max(0.3, 0.0254 * float(m.group(1)))
            drill_mm = max(0.15, 0.0254 * float(m.group(2)))
    layers = tuple(copper[:2]) if len(copper) >= 2 else tuple(copper or ["F.Cu"])
    vias.append(
        Via(x=x, y=y, net=net, size_mm=size_mm, drill_mm=drill_mm, layers=layers)
    )


def import_ses_to_pcb(
    ses_path: str | Path,
    pcb_path: str | Path,
    out_pcb: str | Path,
    *,
    copper_layers: list[str] | None = None,
    clear_existing: bool = True,
    origin_mm: tuple[float, float] = (0.0, 0.0),
) -> Path:
    """Parse SES and append copper to a copy of ``pcb_path``."""
    route = parse_ses_to_route(
        ses_path, copper_layers=copper_layers, origin_mm=origin_mm
    )
    append_routes_to_kicad_pcb(
        str(pcb_path),
        str(out_pcb),
        route,
        clear_existing_copper=clear_existing,
        replace_previous=clear_existing,
    )
    return Path(out_pcb)
