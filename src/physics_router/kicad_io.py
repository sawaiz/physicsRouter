"""Minimal KiCad PCB reader/writer for placement (S-expression based)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from physics_router.models import BoardModel, Component, PlacementConfig


Token = str | list["Token"]


def _tokenize_to_tokens(text: str) -> list[str]:
    # Split keeping parentheses as tokens; strings in quotes stay whole.
    pattern = r'"[^"]*"|[()]|[^\s()]+'
    return re.findall(pattern, text)


def parse_sexpr(text: str) -> Any:
    """Parse a KiCad-like S-expression into nested Python lists."""
    tokens = _tokenize(text)
    pos = 0

    def read() -> Any:
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError("Unexpected end of S-expression")
        tok = tokens[pos]
        pos += 1
        if tok == "(":
            node: list[Any] = []
            while pos < len(tokens) and tokens[pos] != ")":
                node.append(read())
            if pos >= len(tokens):
                raise ValueError("Unclosed list in S-expression")
            pos += 1  # skip ')'
            return node
        if tok == ")":
            raise ValueError("Unexpected ')'")
        if tok.startswith('"') and tok.endswith('"'):
            return tok[1:-1]
        return tok

    root = read()
    return root


def _tokenize_to_tokens_safe(text: str) -> list[str]:
    return _tokenize_to_tokens(text)


def _tokenize(text: str) -> list[str]:
    # Strip comments (lines starting with optional space then # not used in kicad much)
    return _tokenize_scan(text)


def _tokenize_scan(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if c in "()":
            tokens.append(c)
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            tokens.append(text[i : j + 1])
            i = j + 1
            continue
        j = i
        while j < n and not text[j].isspace() and text[j] not in "()":
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


def _find_all(node: Any, head: str) -> list[list[Any]]:
    out: list[list[Any]] = []
    if isinstance(node, list) and node:
        if node[0] == head:
            out.append(node)
        for child in node[1:]:
            out.extend(_find_all(child, head))
    return out


def _find_first(node: Any, head: str) -> list[Any] | None:
    found = _find_all(node, head)
    return found[0] if found else None


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def local_to_board(
    fx: float,
    fy: float,
    frot_deg: float,
    lx: float,
    ly: float,
) -> tuple[float, float]:
    """Map footprint-local mm → board mm (matches pcbnew pad absolute positions).

    KiCad stores ``(at x y rot)`` with *rot* as the footprint orientation.  When
    treating board XY as mathematical Y-up, the correct local→board map uses
    **−rot** with the standard CCW matrix (equivalent to CW by *rot*).  Using
    +rot swaps pads on ±90° parts (LEDs, MK1, S1) — looks “180° out”.
    """
    import math

    th = math.radians(-float(frot_deg or 0.0))
    c, s = math.cos(th), math.sin(th)
    return (fx + lx * c - ly * s, fy + lx * s + ly * c)


def pad_corners_board(
    fx: float,
    fy: float,
    frot_deg: float,
    px: float,
    py: float,
    pad_rot_deg: float,
    w: float,
    h: float,
) -> list[tuple[float, float]]:
    """Board-space corners of a rectangular pad (matches pcbnew pad outline).

    Center is placed with :func:`local_to_board`. The pad rectangle is then
    rotated in **board** space by **-pad_rot** (same sign convention as
    footprint rot). Rotating the pad in footprint-local space *before*
    ``local_to_board`` double-applies the footprint angle and leaves pads 90°
    off (LEDs look radial/tangential swapped; S1 pads vertical instead of
    horizontal).
    """
    import math

    cx, cy = local_to_board(fx, fy, frot_deg, px, py)
    th = math.radians(-float(pad_rot_deg or 0.0))
    c, s = math.cos(th), math.sin(th)
    corners: list[tuple[float, float]] = []
    for lx, ly in (
        (-w / 2, -h / 2),
        (w / 2, -h / 2),
        (w / 2, h / 2),
        (-w / 2, h / 2),
    ):
        corners.append((cx + lx * c - ly * s, cy + lx * s + ly * c))
    return corners


def load_board_from_kicad_pcb(
    path: str | Path,
    config: PlacementConfig | None = None,
    *,
    load_rules: bool = True,
) -> BoardModel:
    """Load footprints, positions, nets, and (optionally) KiCad design rules/stackup."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    root = parse_sexpr(text)

    width = config.board_width_mm if config else 100.0
    height = config.board_height_mm if config else 80.0

    # Build the complete Edge.Cuts model before deriving dimensions.  HALO-90
    # is predominantly arc/circle based, so the old line-only bbox silently
    # fell back to 100x80 mm and gave the router a fictitious exterior region.
    outline = _board_outline_graphics(root)
    bbox = _outline_bbox(outline)
    if bbox is not None:
        # Prefer true outline size when available
        width = max(bbox[2] - bbox[0], 1.0)
        height = max(bbox[3] - bbox[1], 1.0)
        if config:
            # Keep config size if larger (margins)
            width = max(width, config.board_width_mm)
            height = max(height, config.board_height_mm)

    components: dict[str, Component] = {}
    nets: dict[str, list[tuple[str, str]]] = {}

    for fp in _find_all(root, "footprint") + _find_all(root, "module"):
        # KiCad 6+: (footprint "Lib:Name" ... (at x y [rot]) (property "Reference" "R1") ...)
        # Legacy: (module Lib:Name ... (fp_text reference R1) ...)
        ref = _footprint_ref(fp)
        if not ref:
            continue
        at = _find_first(fp, "at")
        x = y = rot = 0.0
        if at and len(at) >= 3:
            x, y = _as_float(at[1]), _as_float(at[2])
            if len(at) >= 4:
                rot = _as_float(at[3])
        fp_name = fp[1] if len(fp) > 1 and isinstance(fp[1], str) else ""
        w, h = _estimate_size(fp)
        # Default pose from .kicad_pcb; YAML fixed may override (floorplan).
        locked = False
        if config:
            for fix in config.fixed:
                if fix.ref == ref and fix.locked:
                    locked = True
        pads: list[dict[str, Any]] = []
        for pad in _find_all(fp, "pad"):
            # (pad "1" smd rect (at ...) (size w h) (nets 3 "NETNAME") )  OR (net 3 "NET")
            pad_num = str(pad[1]) if len(pad) > 1 else "?"
            net_name = _pad_net_name(pad)
            pad_geom = _pad_geometry(pad)
            pads.append({"num": pad_num, "net": net_name, **pad_geom})
            if net_name:
                nets.setdefault(net_name, []).append((ref, pad_num))

        graphics = _footprint_graphics(fp)
        components[ref] = Component(
            ref=ref,
            footprint=fp_name,
            width_mm=w,
            height_mm=h,
            x_mm=x,
            y_mm=y,
            rotation_deg=rot,
            locked=locked,
            pads=pads,
            graphics=graphics,
        )

    if config:
        for fix in config.fixed:
            if fix.ref in components:
                c = components[fix.ref]
                # Floorplan: YAML fixed placement overrides PCB pose when given
                c.x_mm = float(fix.x_mm)
                c.y_mm = float(fix.y_mm)
                c.rotation_deg = float(fix.rotation_deg)
                c.locked = bool(fix.locked or c.locked)
                if fix.notes and not c.notes:
                    c.notes = fix.notes
        # Prefix locks (e.g. HALO LED ring D1–D90) — keep PCB coordinates, mark immovable
        # only when no explicit fixed entry already repositioned the ref
        fixed_refs = {f.ref for f in config.fixed}
        prefixes = [p for p in (config.lock_ref_prefixes or []) if p]
        if prefixes:
            for ref, c in components.items():
                if ref in fixed_refs:
                    continue
                if any(ref.startswith(p) for p in prefixes):
                    c.locked = True
        # Prefer config board size when larger (generous outline / floorplan)
        if config.board_width_mm and config.board_width_mm > width:
            width = float(config.board_width_mm)
        if config.board_height_mm and config.board_height_mm > height:
            height = float(config.board_height_mm)

    copper_layers = ["F.Cu", "B.Cu"]
    rules_dict = None
    if load_rules:
        from physics_router.design_rules import load_design_rules

        dr = load_design_rules(pcb_path=path)
        copper_layers = list(dr.copper_layers) or copper_layers
        rules_dict = dr.summary()

    # Generous rectangular outline when config expands the board envelope
    if config and (
        abs(width - (config.board_width_mm or width)) < 1e-6
        and abs(height - (config.board_height_mm or height)) < 1e-6
        and (width > 110 or height > 90)
    ):
        # Replace tight PCB Edge.Cuts with config-sized rectangle if expanded
        if config.board_width_mm >= 120 or config.board_height_mm >= 100:
            outline = [
                {
                    "kind": "line",
                    "layer": "Edge.Cuts",
                    "x1": 0.0,
                    "y1": 0.0,
                    "x2": width,
                    "y2": 0.0,
                    "width": 0.2,
                },
                {
                    "kind": "line",
                    "layer": "Edge.Cuts",
                    "x1": width,
                    "y1": 0.0,
                    "x2": width,
                    "y2": height,
                    "width": 0.2,
                },
                {
                    "kind": "line",
                    "layer": "Edge.Cuts",
                    "x1": width,
                    "y1": height,
                    "x2": 0.0,
                    "y2": height,
                    "width": 0.2,
                },
                {
                    "kind": "line",
                    "layer": "Edge.Cuts",
                    "x1": 0.0,
                    "y1": height,
                    "x2": 0.0,
                    "y2": 0.0,
                    "width": 0.2,
                },
            ]

    zones = _extract_zones(root, copper_layers)

    return BoardModel(
        width_mm=width,
        height_mm=height,
        components=components,
        nets=nets,
        source_path=str(path),
        design_rules=rules_dict,
        copper_layers=copper_layers,
        outline=outline,
        zones=zones,
    )


def _extract_zones(root: Any, copper_layers: list[str]) -> list[dict[str, Any]]:
    """Parse KiCad ``(zone …)`` copper pours / keepouts into obstacle polygons.

    Filled polygons and zone outlines become routing obstacles. Same-net copper
    may pass (net tag); pure keepouts use ``net=None``.
    """
    zones: list[dict[str, Any]] = []
    for z in _find_all(root, "zone"):
        net_name = ""
        nn = _find_first(z, "net_name")
        if nn and len(nn) >= 2:
            net_name = str(nn[1] or "")
        layer = ""
        ly = _find_first(z, "layer")
        if ly and len(ly) >= 2:
            layer = str(ly[1])
        layers: list[str] = []
        lys = _find_first(z, "layers")
        if lys:
            layers = [str(x) for x in lys[1:] if isinstance(x, (str, int, float))]
        if layer and layer not in layers:
            layers.append(layer)
        if not layers:
            layers = list(copper_layers)

        keepout = False
        ko = _find_first(z, "keepout")
        if ko is not None:
            keepout = True
            # (keepout (tracks not_allowed) …) etc.
            for child in ko[1:] if isinstance(ko, list) else []:
                if isinstance(child, list) and child and str(child[0]) == "tracks":
                    if len(child) >= 2 and str(child[1]) in ("allowed", "yes"):
                        keepout = False

        # Prefer filled_polygon (actual copper), fall back to polygon outline
        polys: list[list[tuple[float, float]]] = []
        for tag in ("filled_polygon", "polygon"):
            for poly in _find_all(z, tag):
                pts_node = _find_first(poly, "pts")
                if not pts_node:
                    continue
                pts: list[tuple[float, float]] = []
                for child in pts_node[1:]:
                    if (
                        isinstance(child, list)
                        and child
                        and str(child[0]) in ("xy", "xyz")
                        and len(child) >= 3
                    ):
                        pts.append((_as_float(child[1]), _as_float(child[2])))
                if len(pts) >= 3:
                    polys.append(pts)
            if polys:
                break

        if not polys:
            continue
        for pts in polys:
            for ly_name in layers:
                zones.append(
                    {
                        "net": None if keepout else (net_name or None),
                        "layer": str(ly_name),
                        "keepout": keepout,
                        "points": [[float(x), float(y)] for x, y in pts],
                        "filled": True,
                    }
                )
    return zones


def _layer_name(node: list[Any]) -> str:
    ly = _find_first(node, "layer")
    if ly and len(ly) >= 2:
        return str(ly[1])
    # pads use (layers "F.Cu" "F.Paste" ...)
    lys = _find_first(node, "layers")
    if lys and len(lys) >= 2:
        return str(lys[1])
    return ""


def _width_mm(node: list[Any], default: float = 0.1) -> float:
    w = _find_first(node, "width")
    if w and len(w) >= 2:
        return abs(_as_float(w[1], default))
    w = _find_first(node, "stroke")
    if w:
        ww = _find_first(w, "width")
        if ww and len(ww) >= 2:
            return abs(_as_float(ww[1], default))
    return default


def _pad_geometry(pad: list[Any]) -> dict[str, Any]:
    """Local pad geometry for accurate 2D footprint rendering.

    KiCad pad form: (pad "1" smd|thru_hole rect|circle|roundrect|oval (at ...) (size ...) ...)
    """
    # pad[2]=type (smd/thru_hole), pad[3]=shape when present as bare atom
    pad_type = str(pad[2]) if len(pad) > 2 and not isinstance(pad[2], list) else "smd"
    shape = "rect"
    if len(pad) > 3 and not isinstance(pad[3], list):
        shape = str(pad[3])
    at = _find_first(pad, "at")
    ax = ay = arot = 0.0
    if at and len(at) >= 3:
        ax, ay = _as_float(at[1]), _as_float(at[2])
        if len(at) >= 4:
            arot = _as_float(at[3])
    size = _find_first(pad, "size")
    sw = sh = 0.5
    if size and len(size) >= 3:
        sw, sh = abs(_as_float(size[1], 0.5)), abs(_as_float(size[2], 0.5))
    drill = _find_first(pad, "drill")
    drill_mm = 0.0
    if drill and len(drill) >= 2:
        # (drill 0.3) or (drill oval 0.3 0.4)
        try:
            drill_mm = abs(float(drill[1]))
        except (TypeError, ValueError):
            if len(drill) >= 3:
                drill_mm = abs(_as_float(drill[2]))
    layers: list[str] = []
    lys = _find_first(pad, "layers")
    if lys:
        layers = [str(x) for x in lys[1:] if isinstance(x, str)]
    pinfunction = ""
    pf = _find_first(pad, "pinfunction")
    if pf and len(pf) >= 2:
        pinfunction = str(pf[1])
    custom_strokes: list[dict[str, Any]] = []
    primitives = _find_first(pad, "primitives")
    if primitives is not None:
        for arc in _find_all(primitives, "gr_arc"):
            start = _find_first(arc, "start")
            end = _find_first(arc, "end")
            angle = _find_first(arc, "angle")
            if start and end and angle and len(start) >= 3 and len(end) >= 3:
                custom_strokes.append(
                    {
                        "pts": _arc_to_polyline(
                            _as_float(start[1]),
                            _as_float(start[2]),
                            _as_float(end[1]),
                            _as_float(end[2]),
                            _as_float(angle[1]),
                        ),
                        "width": _width_mm(arc, 0.0),
                    }
                )
        for line in _find_all(primitives, "gr_line"):
            start = _find_first(line, "start")
            end = _find_first(line, "end")
            if start and end and len(start) >= 3 and len(end) >= 3:
                custom_strokes.append(
                    {
                        "pts": [
                            [_as_float(start[1]), _as_float(start[2])],
                            [_as_float(end[1]), _as_float(end[2])],
                        ],
                        "width": _width_mm(line, 0.0),
                    }
                )
    return {
        "shape": shape,
        "type": pad_type,
        "x": ax,
        "y": ay,
        "rot": arot,
        "w": sw,
        "h": sh,
        "drill": drill_mm,
        "layers": layers,
        "pinfunction": pinfunction,
        "custom_strokes": custom_strokes,
    }


def _footprint_graphics(fp: list[Any]) -> list[dict[str, Any]]:
    """Extract local-coordinate silk/fab/copper outline graphics from a footprint."""
    gfx: list[dict[str, Any]] = []
    for line in _find_all(fp, "fp_line"):
        layer = _layer_name(line)
        if layer and not any(w in layer for w in ("Silk", "Fab", "CrtYd", ".Cu")):
            continue
        st = _find_first(line, "start")
        en = _find_first(line, "end")
        if not st or not en or len(st) < 3 or len(en) < 3:
            continue
        gfx.append({
            "kind": "line",
            "layer": layer or "F.SilkS",
            "x1": _as_float(st[1]), "y1": _as_float(st[2]),
            "x2": _as_float(en[1]), "y2": _as_float(en[2]),
            "width": _width_mm(line, 0.12),
        })

    for rect in _find_all(fp, "fp_rect"):
        layer = _layer_name(rect)
        if layer and not any(w in layer for w in ("Silk", "Fab", "CrtYd")):
            continue
        st = _find_first(rect, "start")
        en = _find_first(rect, "end")
        if not st or not en or len(st) < 3 or len(en) < 3:
            continue
        fill = _find_first(rect, "fill")
        filled = bool(fill and len(fill) >= 2 and str(fill[1]) not in ("none", "no"))
        gfx.append({
            "kind": "rect",
            "layer": layer or "F.SilkS",
            "x1": _as_float(st[1]), "y1": _as_float(st[2]),
            "x2": _as_float(en[1]), "y2": _as_float(en[2]),
            "width": _width_mm(rect, 0.12),
            "fill": filled,
        })

    for circ in _find_all(fp, "fp_circle"):
        layer = _layer_name(circ)
        if layer and not any(w in layer for w in ("Silk", "Fab", "CrtYd", ".Cu")):
            continue
        ctr = _find_first(circ, "center")
        end = _find_first(circ, "end")
        if not ctr or not end or len(ctr) < 3 or len(end) < 3:
            continue
        cx, cy = _as_float(ctr[1]), _as_float(ctr[2])
        ex, ey = _as_float(end[1]), _as_float(end[2])
        r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
        fill = _find_first(circ, "fill")
        filled = bool(fill and len(fill) >= 2 and str(fill[1]) not in ("none", "no"))
        gfx.append({
            "kind": "circle",
            "layer": layer or "F.SilkS",
            "cx": cx, "cy": cy, "r": r,
            "width": _width_mm(circ, 0.12),
            "fill": filled,
        })

    for poly in _find_all(fp, "fp_poly"):
        layer = _layer_name(poly)
        if layer and not any(w in layer for w in ("Silk", "Fab", "CrtYd", ".Cu")):
            continue
        pts_node = _find_first(poly, "pts")
        pts: list[list[float]] = []
        if pts_node:
            for child in pts_node[1:]:
                if isinstance(child, list) and child and child[0] == "xy" and len(child) >= 3:
                    pts.append([_as_float(child[1]), _as_float(child[2])])
        if len(pts) >= 2:
            fill = _find_first(poly, "fill")
            filled = bool(fill and len(fill) >= 2 and str(fill[1]) not in ("none", "no"))
            gfx.append({
                "kind": "poly",
                "layer": layer or "F.SilkS",
                "pts": pts,
                "width": _width_mm(poly, 0.1),
                "fill": filled,
            })

    # Pad copper as graphics so the viewer can draw real pad shapes
    for pad in _find_all(fp, "pad"):
        g = _pad_geometry(pad)
        layers = g.get("layers") or ["F.Cu"]
        copper_layers = [ly for ly in layers if ".Cu" in ly or ly in ("*.Cu", "F&B.Cu")]
        if not copper_layers:
            copper_layers = ["F.Cu"]
        for ly in copper_layers[:2]:  # front + back enough for 2D
            gfx.append({
                "kind": "pad",
                "layer": ly if ly != "*.Cu" else "F.Cu",
                "shape": g["shape"],
                "x": g["x"], "y": g["y"], "rot": g["rot"],
                "w": g["w"], "h": g["h"],
                "drill": g.get("drill") or 0.0,
                "num": str(pad[1]) if len(pad) > 1 else "",
                "pinfunction": g.get("pinfunction") or "",
            })

    # Cap size for huge footprints
    if len(gfx) > 400:
        gfx = gfx[:400]
    return gfx


def _arc_to_polyline(
    cx: float,
    cy: float,
    x_file_end: float,
    y_file_end: float,
    angle_deg: float,
    *,
    n: int = 48,
) -> list[list[float]]:
    """Sample classic KiCad ``gr_arc`` / ``fp_arc``.

    File form: ``(gr_arc (start cx cy) (end x y) (angle deg))`` where
    ``start`` is the **center**, ``end`` is the **arc start point**, and the arc
    sweeps ``angle`` degrees CCW (negative = CW) to the arc end. Matches pcbnew
    ``GetArcStart``/``GetArcEnd`` on loaded boards.
    """
    import math

    r = math.hypot(x_file_end - cx, y_file_end - cy)
    if r < 1e-9:
        return [[cx, cy]]
    # File "end" token is the arc *start* point on the circle.
    a_start = math.atan2(y_file_end - cy, x_file_end - cx)
    sweep = math.radians(angle_deg)
    pts: list[list[float]] = []
    steps = max(8, int(abs(angle_deg) / 4) + 1)
    steps = min(steps, n)
    for i in range(steps + 1):
        t = i / steps
        a = a_start + sweep * t
        pts.append([cx + r * math.cos(a), cy + r * math.sin(a)])
    return pts


def _board_outline_graphics(root: Any) -> list[dict[str, Any]]:
    """Edge.Cuts lines/arcs/circles in board coordinates."""
    out: list[dict[str, Any]] = []
    for tag in ("gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly"):
        for gr in _find_all(root, tag):
            layer = _layer_name(gr)
            if "Edge.Cuts" not in layer and "Edge_Cuts" not in layer:
                continue
            if tag == "gr_line":
                st = _find_first(gr, "start")
                en = _find_first(gr, "end")
                if st and en and len(st) >= 3 and len(en) >= 3:
                    out.append({
                        "kind": "line",
                        "layer": "Edge.Cuts",
                        "x1": _as_float(st[1]), "y1": _as_float(st[2]),
                        "x2": _as_float(en[1]), "y2": _as_float(en[2]),
                        "width": max(_width_mm(gr, 0.1), 0.15),
                    })
            elif tag == "gr_circle":
                ctr = _find_first(gr, "center")
                end = _find_first(gr, "end")
                if ctr and end and len(ctr) >= 3 and len(end) >= 3:
                    cx, cy = _as_float(ctr[1]), _as_float(ctr[2])
                    ex, ey = _as_float(end[1]), _as_float(end[2])
                    r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
                    out.append({
                        "kind": "circle",
                        "layer": "Edge.Cuts",
                        "cx": cx, "cy": cy, "r": r,
                        "width": max(_width_mm(gr, 0.1), 0.15),
                        "fill": False,
                    })
            elif tag == "gr_rect":
                st = _find_first(gr, "start")
                en = _find_first(gr, "end")
                if st and en and len(st) >= 3 and len(en) >= 3:
                    out.append({
                        "kind": "rect",
                        "layer": "Edge.Cuts",
                        "x1": _as_float(st[1]), "y1": _as_float(st[2]),
                        "x2": _as_float(en[1]), "y2": _as_float(en[2]),
                        "width": max(_width_mm(gr, 0.1), 0.15),
                        "fill": False,
                    })
            elif tag == "gr_arc":
                # Classic: (gr_arc (start cx cy) (end x y) (angle deg))
                #   start=center, end=arc-start point, angle=CCW sweep (see _arc_to_polyline)
                # Modern:  (gr_arc (start x y) (mid x y) (end x y))
                st = _find_first(gr, "start")
                mid = _find_first(gr, "mid")
                en = _find_first(gr, "end")
                ang = _find_first(gr, "angle")
                if st and en and len(st) >= 3 and len(en) >= 3 and ang and len(ang) >= 2:
                    cx, cy = _as_float(st[1]), _as_float(st[2])
                    ex, ey = _as_float(en[1]), _as_float(en[2])
                    a_deg = _as_float(ang[1])
                    pts = _arc_to_polyline(cx, cy, ex, ey, a_deg)
                    out.append({
                        "kind": "poly",
                        "layer": "Edge.Cuts",
                        "pts": pts,
                        "width": max(_width_mm(gr, 0.1), 0.15),
                        "fill": False,
                        "closed": False,
                    })
                    # Full 360° arc → circle (rare on Edge.Cuts)
                    if abs(abs(a_deg) - 360) < 1:
                        r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
                        out.append({
                            "kind": "circle",
                            "layer": "Edge.Cuts",
                            "cx": cx, "cy": cy, "r": r,
                            "width": max(_width_mm(gr, 0.1), 0.15),
                            "fill": False,
                        })
                elif st and en and len(st) >= 3 and len(en) >= 3:
                    item: dict[str, Any] = {
                        "kind": "arc",
                        "layer": "Edge.Cuts",
                        "x1": _as_float(st[1]), "y1": _as_float(st[2]),
                        "x2": _as_float(en[1]), "y2": _as_float(en[2]),
                        "width": max(_width_mm(gr, 0.1), 0.15),
                    }
                    if mid and len(mid) >= 3:
                        item["mx"] = _as_float(mid[1])
                        item["my"] = _as_float(mid[2])
                    out.append(item)

    # If classic arcs form a near-circle around origin (HALO disk r≈12), add a full
    # circle so the Edge.Cuts outline is continuous in the viewer.
    # Prefer radii of origin-centered arc samples (main disk), NOT the hook tip
    # which sits farther out and would inflate max_r (~13.6 vs 12).
    origin_radii: list[float] = []
    for g in out:
        if g.get("kind") == "poly" and g.get("pts"):
            for p in g["pts"]:
                r = (p[0] ** 2 + p[1] ** 2) ** 0.5
                # samples that sit on a disk around origin (not hook tip ~13.6)
                if 8.0 < r < 12.8:
                    origin_radii.append(r)
        if g.get("kind") == "circle":
            r = float(g.get("r") or 0)
            cx, cy = float(g.get("cx") or 0), float(g.get("cy") or 0)
            if (cx * cx + cy * cy) ** 0.5 < 0.5 and 8.0 < r < 20.0:
                origin_radii.append(r)
    r_disk = 0.0
    if origin_radii:
        origin_radii.sort()
        # median of origin-disk samples
        mid = origin_radii[len(origin_radii) // 2]
        r_disk = 12.0 if abs(mid - 12.0) < 0.5 else mid
    if 8.0 < r_disk < 20.0 and not any(
        g.get("kind") == "circle"
        and abs(float(g.get("cx") or 0)) < 0.5
        and abs(float(g.get("cy") or 0)) < 0.5
        and abs(float(g.get("r") or 0) - r_disk) < 0.5
        for g in out
    ):
        out.insert(
            0,
            {
                "kind": "circle",
                "layer": "Edge.Cuts",
                "cx": 0.0,
                "cy": 0.0,
                "r": r_disk,
                "width": 0.15,
                "fill": False,
            },
        )
    return out


def _footprint_ref(fp: list[Any]) -> str | None:
    for prop in _find_all(fp, "property"):
        # (property "Reference" "R1" ...)
        if len(prop) >= 3 and prop[1] == "Reference":
            return str(prop[2])
    for t in _find_all(fp, "fp_text"):
        # (fp_text reference R1 ...)
        if len(t) >= 3 and t[1] == "reference":
            return str(t[2])
    return None


def _pad_net_name(pad: list[Any]) -> str | None:
    for child in pad:
        if isinstance(child, list) and child and child[0] == "net" and len(child) >= 3:
            return str(child[2])
        if isinstance(child, list) and child and child[0] == "nets" and len(child) >= 3:
            return str(child[2])
    return None


def _estimate_size(fp: list[Any]) -> tuple[float, float]:
    """Footprint AABB from pads (local coords). Prefer courtyard if present."""
    sizes: list[tuple[float, float]] = []
    # Courtyard / fab outline first (accurate for 0402 LEDs etc.)
    for line in _find_all(fp, "fp_line"):
        layer = _find_first(line, "layer")
        layer_s = str(layer[1]) if layer and len(layer) >= 2 else ""
        if "CrtYd" not in layer_s and "Fab" not in layer_s:
            continue
        for tag in ("start", "end"):
            pt = _find_first(line, tag)
            if pt and len(pt) >= 3:
                sizes.append((_as_float(pt[1]), _as_float(pt[2])))
    if len(sizes) >= 2:
        xs = [p[0] for p in sizes]
        ys = [p[1] for p in sizes]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if w > 0.05 and h > 0.05:
            return w, h

    sizes = []
    for pad in _find_all(fp, "pad"):
        size = _find_first(pad, "size")
        at = _find_first(pad, "at")
        if size and len(size) >= 3 and at and len(at) >= 3:
            sw, sh = abs(_as_float(size[1])), abs(_as_float(size[2]))
            ax, ay = _as_float(at[1]), _as_float(at[2])
            sizes.append((ax - sw / 2, ay - sh / 2))
            sizes.append((ax + sw / 2, ay + sh / 2))
    if not sizes:
        return 2.0, 2.0
    xs = [p[0] for p in sizes]
    ys = [p[1] for p in sizes]
    # Do not force 1mm minimum — that made 0402 LEDs nearly square and look
    # mis-rotated on the HALO ring (true body ~1.0×0.5 mm).
    return max(0.2, max(xs) - min(xs)), max(0.2, max(ys) - min(ys))


def _edge_bbox(root: Any) -> tuple[float, float, float, float] | None:
    """Compatibility wrapper returning bounds for every Edge.Cuts primitive."""
    return _outline_bbox(_board_outline_graphics(root))


def _outline_bbox(
    outline: list[dict[str, Any]],
) -> tuple[float, float, float, float] | None:
    """Exact-enough bbox for sampled lines, polygons, rectangles, and circles."""
    xs: list[float] = []
    ys: list[float] = []
    for item in outline:
        kind = str(item.get("kind") or "")
        if kind == "circle":
            cx = float(item.get("cx") or 0.0)
            cy = float(item.get("cy") or 0.0)
            radius = abs(float(item.get("r") or 0.0))
            xs.extend((cx - radius, cx + radius))
            ys.extend((cy - radius, cy + radius))
        elif kind == "poly":
            for point in item.get("pts") or []:
                if len(point) >= 2:
                    xs.append(float(point[0]))
                    ys.append(float(point[1]))
        elif kind in ("line", "rect", "arc"):
            for x_key, y_key in (("x1", "y1"), ("x2", "y2"), ("mx", "my")):
                if x_key in item and y_key in item:
                    xs.append(float(item[x_key]))
                    ys.append(float(item[y_key]))
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def apply_placement_to_kicad_pcb(
    source_path: str | Path,
    positions: dict[str, tuple[float, float, float]],
    dest_path: str | Path,
) -> None:
    """Rewrite footprint (at x y rot) for refs present in positions.

    Text-level rewrite avoids full AST round-trip fidelity issues.
    """
    source_path = Path(source_path)
    dest_path = Path(dest_path)
    text = source_path.read_text(encoding="utf-8", errors="replace")

    for ref, (x, y, rot) in positions.items():
        text = _rewrite_footprint_at(text, ref, x, y, rot)

    dest_path.write_text(text, encoding="utf-8")


def _rewrite_footprint_at(text: str, ref: str, x: float, y: float, rot: float) -> str:
    """Locate footprint block containing Reference=ref and replace its (at ...)."""
    # Find property "Reference" "REF" then search backwards for footprint start
    patterns = [
        rf'\(property\s+"Reference"\s+"{re.escape(ref)}"',
        rf'\(fp_text\s+reference\s+{re.escape(ref)}\b',
    ]
    ref_pos = -1
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            ref_pos = m.start()
            break
    if ref_pos < 0:
        return text

    # Walk back to nearest "(footprint" or "(module"
    start = text.rfind("(footprint", 0, ref_pos)
    start_mod = text.rfind("(module", 0, ref_pos)
    start = max(start, start_mod)
    if start < 0:
        return text

    # Find matching close paren for this footprint (simple depth scan)
    depth = 0
    end = start
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    block = text[start:end]
    at_re = re.compile(r"\(at\s+[-\d.eE+]+\s+[-\d.eE+]+(?:\s+[-\d.eE+]+)?\)")
    new_at = f"(at {x:.4f} {y:.4f} {rot:.2f})"
    new_block, n = at_re.subn(new_at, block, count=1)
    if n == 0:
        # insert after footprint name line
        new_block = re.sub(
            r"(\((?:footprint|module)\s+[^\n]+)",
            r"\1\n\t\t" + new_at,
            block,
            count=1,
        )
    return text[:start] + new_block + text[end:]


def board_from_synthetic(config: PlacementConfig) -> BoardModel:
    """Build a synthetic board from config when no .kicad_pcb is available (tests/demo).

    Pads include local offsets + copper layers so pin-access / ExactMap paths
    exercise the same geometry as real KiCad loads.
    """
    def _pad(num: str, net: str, x: float = 0.0, y: float = 0.0, w: float = 0.6, h: float = 0.6) -> dict:
        return {
            "num": num,
            "net": net,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "rot": 0.0,
            "shape": "rect",
            "type": "smd",
            "drill": 0.0,
            "layers": ["F.Cu"],
        }

    comps = {
        "U1": Component(
            ref="U1",
            footprint="Package_SO:SOIC-8",
            width_mm=5.0,
            height_mm=4.0,
            x_mm=15,
            y_mm=20,
            power_dissipation_w=0.5,
            pads=[
                _pad("1", "+5V", -2.0, 1.2),
                _pad("2", "GND", -2.0, -1.2),
                _pad("SW", "SW", 2.0, 0.0),
            ],
        ),
        "L1": Component(
            ref="L1",
            footprint="Inductor_SMD:L_0805",
            width_mm=2.0,
            height_mm=1.2,
            x_mm=20,
            y_mm=18,
            pads=[_pad("1", "SW", -0.7, 0.0), _pad("2", "+5V", 0.7, 0.0)],
        ),
        "C_IN": Component(
            ref="C_IN",
            footprint="Capacitor_SMD:C_0805",
            width_mm=2.0,
            height_mm=1.2,
            x_mm=12,
            y_mm=15,
            pads=[_pad("1", "+5V", -0.7, 0.0), _pad("2", "GND", 0.7, 0.0)],
        ),
        "C_OUT": Component(
            ref="C_OUT",
            footprint="Capacitor_SMD:C_0805",
            width_mm=2.0,
            height_mm=1.2,
            x_mm=25,
            y_mm=15,
            pads=[_pad("1", "+5V", -0.7, 0.0), _pad("2", "GND", 0.7, 0.0)],
        ),
        "D1": Component(
            ref="D1",
            footprint="Diode_SMD:D_SOD-123",
            width_mm=2.5,
            height_mm=1.5,
            x_mm=18,
            y_mm=25,
            pads=[_pad("A", "SW", -0.8, 0.0), _pad("K", "GND", 0.8, 0.0)],
        ),
        "X1": Component(
            ref="X1",
            footprint="Crystal:Crystal_SMD",
            width_mm=3.2,
            height_mm=2.5,
            x_mm=35,
            y_mm=30,
            pads=[_pad("1", "CLK_MCU", -1.0, 0.0), _pad("2", "GND", 1.0, 0.0)],
        ),
        "U2": Component(
            ref="U2",
            footprint="Package_QFP:LQFP-48",
            width_mm=9.0,
            height_mm=9.0,
            x_mm=35,
            y_mm=25,
            power_dissipation_w=0.2,
            pads=[
                _pad("VDD", "+5V", -3.5, 3.0),
                _pad("GND", "GND", 3.5, 3.0),
                _pad("OSC_IN", "CLK_MCU", -3.5, -3.0),
                _pad("USB_DP", "USB_DP", 3.5, -1.0),
                _pad("USB_DM", "USB_DM", 3.5, 1.0),
                _pad("PA0", "AIN0", -3.5, 0.0),
            ],
        ),
        "J1": Component(
            ref="J1",
            footprint="Connector:USB_C",
            width_mm=9.0,
            height_mm=8.0,
            x_mm=2.0,
            y_mm=20.0,
            locked=True,
            pads=[
                _pad("GND", "GND", 0.0, -2.0),
                _pad("D+", "USB_DP", 2.0, 0.0),
                _pad("D-", "USB_DM", 2.0, 1.5),
            ],
        ),
        "R_AIN": Component(
            ref="R_AIN",
            footprint="Resistor_SMD:R_0603",
            width_mm=1.6,
            height_mm=0.8,
            x_mm=40,
            y_mm=10,
            pads=[_pad("1", "AIN0", -0.5, 0.0), _pad("2", "GND", 0.5, 0.0)],
        ),
    }
    for fix in config.fixed:
        if fix.ref in comps:
            comps[fix.ref].x_mm = fix.x_mm
            comps[fix.ref].y_mm = fix.y_mm
            comps[fix.ref].rotation_deg = fix.rotation_deg
            comps[fix.ref].locked = fix.locked

    nets = {
        "+5V": [("U1", "1"), ("C_OUT", "1"), ("U2", "VDD")],
        "GND": [("U1", "2"), ("C_IN", "2"), ("C_OUT", "2"), ("U2", "GND"), ("J1", "GND")],
        "SW": [("U1", "SW"), ("L1", "1"), ("D1", "A")],
        "CLK_MCU": [("U2", "OSC_IN"), ("X1", "1")],
        "USB_DP": [("J1", "D+"), ("U2", "USB_DP")],
        "USB_DM": [("J1", "D-"), ("U2", "USB_DM")],
        "AIN0": [("U2", "PA0"), ("R_AIN", "1")],
    }
    # Outline rectangle for Edge.Cuts-aware routing
    w, h = float(config.board_width_mm), float(config.board_height_mm)
    outline = [
        {"kind": "line", "layer": "Edge.Cuts", "x1": 0, "y1": 0, "x2": w, "y2": 0, "width": 0.1},
        {"kind": "line", "layer": "Edge.Cuts", "x1": w, "y1": 0, "x2": w, "y2": h, "width": 0.1},
        {"kind": "line", "layer": "Edge.Cuts", "x1": w, "y1": h, "x2": 0, "y2": h, "width": 0.1},
        {"kind": "line", "layer": "Edge.Cuts", "x1": 0, "y1": h, "x2": 0, "y2": 0, "width": 0.1},
    ]
    return BoardModel(
        width_mm=w,
        height_mm=h,
        components=comps,
        nets=nets,
        source_path=None,
        copper_layers=["F.Cu", "B.Cu"],
        outline=outline,
    )
