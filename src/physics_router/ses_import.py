"""Import FreeRouting / Specctra SES wiring into RouteResult or KiCad PCB.

Parses a minimal subset of Specctra session files:
  (wire (path Signal_0 width x1 y1 x2 y2 ...) (net "NAME") ...)
  (via ViaName x y (net "NAME") ...)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from physics_router.kicad_io import parse_sexpr
from physics_router.router import RouteResult, RouteSegment, Via, append_routes_to_kicad_pcb


def _mil_to_mm(v: float) -> float:
    return float(v) * 0.0254


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _find_all(node: Any, head: str) -> list[list[Any]]:
    out: list[list[Any]] = []
    if isinstance(node, list) and node:
        if node[0] == head:
            out.append(node)
        for child in node[1:]:
            out.extend(_find_all(child, head))
    return out


def _find_first(node: Any, head: str) -> list[Any] | None:
    if isinstance(node, list) and node:
        if node[0] == head:
            return node
        for child in node[1:]:
            hit = _find_first(child, head)
            if hit is not None:
                return hit
    return None


def _layer_from_token(tok: str, copper: list[str]) -> str:
    m = re.match(r"Signal_(\d+)", str(tok), re.I)
    if m:
        i = int(m.group(1))
        if 0 <= i < len(copper):
            return copper[i]
    # Direct KiCad-ish name
    if str(tok) in copper:
        return str(tok)
    return copper[0] if copper else "F.Cu"


def parse_ses_to_route(
    ses_path: str | Path,
    *,
    copper_layers: list[str] | None = None,
    origin_mm: tuple[float, float] = (0.0, 0.0),
) -> RouteResult:
    """Parse SES wiring into a :class:`RouteResult` (mil → mm, origin shift)."""
    copper = list(copper_layers or ["F.Cu", "B.Cu"])
    text = Path(ses_path).read_text(encoding="utf-8", errors="replace")
    # FreeRouting SES sometimes uses unquoted tokens; parser still works
    root = parse_sexpr(text)
    ox, oy = origin_mm
    segs: list[RouteSegment] = []
    vias: list[Via] = []

    for wire in _find_all(root, "wire"):
        net = ""
        nn = _find_first(wire, "net")
        if nn and len(nn) >= 2:
            net = str(nn[1])
        path = _find_first(wire, "path")
        if not path or len(path) < 5:
            continue
        # (path layer width x1 y1 x2 y2 ...)
        layer = _layer_from_token(str(path[1]), copper)
        width = _mil_to_mm(_as_float(path[2], 10))
        coords = path[3:]
        pts: list[tuple[float, float]] = []
        i = 0
        while i + 1 < len(coords):
            if isinstance(coords[i], list):
                i += 1
                continue
            x = _mil_to_mm(_as_float(coords[i])) + ox
            y = _mil_to_mm(_as_float(coords[i + 1])) + oy
            pts.append((x, y))
            i += 2
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
                    width_mm=max(width, 0.1),
                )
            )

    for via in _find_all(root, "via"):
        # (via "ViaName" x y (net "N") ...)  or (via ViaName x y)
        if len(via) < 4:
            continue
        try:
            x = _mil_to_mm(_as_float(via[2])) + ox
            y = _mil_to_mm(_as_float(via[3])) + oy
        except (TypeError, ValueError, IndexError):
            continue
        net = ""
        nn = _find_first(via, "net")
        if nn and len(nn) >= 2:
            net = str(nn[1])
        vias.append(
            Via(
                x=x,
                y=y,
                net=net or "NET",
                size_mm=0.6,
                drill_mm=0.3,
                layers=tuple(copper[:2]) if len(copper) >= 2 else tuple(copper or ["F.Cu"]),
            )
        )

    result = RouteResult(segments=segs, vias=vias, via_count=len(vias))
    result.total_length_mm = sum(
        ((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) ** 0.5 for s in segs
    )
    result.notes.append(f"imported SES: {len(segs)} segs, {len(vias)} vias from {Path(ses_path).name}")
    result.compute_quality()
    return result


def import_ses_to_pcb(
    ses_path: str | Path,
    pcb_path: str | Path,
    out_pcb: str | Path,
    *,
    copper_layers: list[str] | None = None,
    clear_existing: bool = True,
) -> Path:
    """Parse SES and append copper to a copy of ``pcb_path``."""
    route = parse_ses_to_route(ses_path, copper_layers=copper_layers)
    append_routes_to_kicad_pcb(
        str(pcb_path),
        str(out_pcb),
        route,
        clear_existing_copper=clear_existing,
        replace_previous=clear_existing,
    )
    return Path(out_pcb)
