"""Import FreeRouting / Specctra SES wiring into RouteResult or KiCad PCB.

Parses a practical subset of Specctra session files:

* ``(wire (path LAYER WIDTH x y …) (net "NAME"))``
* ``(wire_fromto …)`` style aliases
* ``(via NAME x y (net …))`` and ``(via x y …)``
* Nested ``(routes (network_out (net …)))`` FreeRouting layout
* Optional resolution unit (mil / mm / inch)
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
    # (resolution mil 10) or (unit mil)
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
    # FreeRouting default is mil
    return 0.0254


def _layer_from_token(tok: str, copper: list[str]) -> str:
    s = str(tok).strip('"')
    m = re.match(r"Signal[_\s-]?(\d+)", s, re.I)
    if m:
        i = int(m.group(1))
        if 0 <= i < len(copper):
            return copper[i]
    # F.Cu / Front / top aliases
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
    # In1.Cu style
    for ly in copper:
        if ly.lower() == low:
            return ly
    return copper[0] if copper else "F.Cu"


def _net_from_node(node: list[Any], fallback: str = "") -> str:
    nn = _find_first(node, "net")
    if nn and len(nn) >= 2:
        return str(nn[1]).strip('"')
    # Parent may be (net "NAME" (wire …))
    return fallback


def _coords_to_points(
    coords: list[Any], scale: float, ox: float, oy: float
) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    i = 0
    while i < len(coords):
        c = coords[i]
        if isinstance(c, list):
            # (xy x y) or nested
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
    # (path layer width x1 y1 x2 y2 …)
    layer = _layer_from_token(str(path[1]), copper)
    width = max(scale * _as_float(path[2], 10 / 0.0254 if scale == 0.0254 else 0.25), 0.05)
    # If width was stored in mil already and scale is mil→mm, path[2] is mil
    if scale == 0.0254:
        width = max(0.0254 * _as_float(path[2], 10), 0.05)
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
    # Strip C-style block comments FreeRouting sometimes embeds
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    root = parse_sexpr(text)
    scale = float(unit_scale_mm) if unit_scale_mm is not None else _detect_unit_scale(root)
    ox, oy = origin_mm
    segs: list[RouteSegment] = []
    vias: list[Via] = []

    # Wires anywhere (top-level or under network_out)
    for wire in _find_all(root, "wire") + _find_all(root, "wire_fromto"):
        # Inherit net from enclosing (net "NAME" …)
        net = _net_from_node(wire)
        path = _find_first(wire, "path")
        if path is None:
            # Some SES put coords directly on wire: (wire layer width x y …)
            if len(wire) >= 6 and not isinstance(wire[1], list):
                path = ["path", wire[1], wire[2], *wire[3:]]
            else:
                continue
        if not net:
            # Walk: freerouting nests (net "X" (wire …))
            pass
        _append_path_segments(
            segs, path, net=net or "NET", copper=copper, scale=scale, ox=ox, oy=oy
        )

    # FreeRouting: (net "NAME" (wire …) (via …)) under network_out
    for net_node in _find_all(root, "net"):
        if len(net_node) < 2 or isinstance(net_node[1], list):
            continue
        net_name = str(net_node[1]).strip('"')
        if not net_name:
            continue
        for wire in _find_all(net_node, "wire") + _find_all(net_node, "wire_fromto"):
            path = _find_first(wire, "path")
            if path is None and len(wire) >= 6 and not isinstance(wire[1], list):
                path = ["path", wire[1], wire[2], *wire[3:]]
            if path is None:
                continue
            _append_path_segments(
                segs, path, net=net_name, copper=copper, scale=scale, ox=ox, oy=oy
            )
        for via in _find_all(net_node, "via"):
            _parse_via(via, vias, copper, scale, ox, oy, default_net=net_name)

    for via in _find_all(root, "via"):
        _parse_via(via, vias, copper, scale, ox, oy, default_net="")

    # Dedup identical segments
    seen: set[tuple] = set()
    uniq_segs: list[RouteSegment] = []
    for s in segs:
        key = (
            round(s.x1, 4),
            round(s.y1, 4),
            round(s.x2, 4),
            round(s.y2, 4),
            s.layer,
            s.net,
        )
        if key in seen:
            continue
        seen.add(key)
        uniq_segs.append(s)

    result = RouteResult(segments=uniq_segs, vias=vias, via_count=len(vias))
    result.total_length_mm = sum(
        ((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) ** 0.5 for s in uniq_segs
    )
    result.notes.append(
        f"imported SES: {len(uniq_segs)} segs, {len(vias)} vias from {Path(ses_path).name} "
        f"(scale={scale} mm/unit)"
    )
    result.compute_quality()
    return result


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
    # Find first two numeric coordinates
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
    # Optional padstack name may encode size
    size_mm, drill_mm = 0.6, 0.3
    if len(via) >= 2 and isinstance(via[1], str) and not via[1].replace(".", "", 1).isdigit():
        m = re.search(r"(\d+):(\d+)", str(via[1]))
        if m:
            # mil diameters in FreeRouting padstack names often
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
