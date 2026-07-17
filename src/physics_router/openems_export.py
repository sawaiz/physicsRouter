"""OpenEMS mesh / geometry export from placement, routes, and Gerbers.

Produces:
- `board_geometry.json` — neutral copper primitives (mm)
- `simulate_board.py` — CSXCAD/openEMS Python driver (run if openEMS+CSXCAD installed)
- Optional Gerber-derived copper polylines

Coordinates: board XY in mm, Z stackup in mm (converted to m in the sim script).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from physics_router.models import BoardModel, PlacementConfig
from physics_router.router import RouteResult, RouteSegment, clearance_aware_route


@dataclass
class StackupLayer:
    name: str
    thickness_mm: float
    material: str = "FR4"  # FR4 | copper | air
    z0_mm: float = 0.0  # bottom of layer


@dataclass
class Stackup:
    layers: list[StackupLayer] = field(default_factory=list)

    @staticmethod
    def default_2layer(dielectric_mm: float = 1.6, copper_mm: float = 0.035) -> Stackup:
        # Z up: B.Cu, dielectric, F.Cu
        return Stackup(
            layers=[
                StackupLayer("B.Cu", copper_mm, "copper", 0.0),
                StackupLayer("dielectric", dielectric_mm, "FR4", copper_mm),
                StackupLayer("F.Cu", copper_mm, "copper", copper_mm + dielectric_mm),
            ]
        )

    @staticmethod
    def from_design_rules(rules: object) -> Stackup:
        """Build OpenEMS stackup from physics_router.design_rules.DesignRules."""
        layers_in = getattr(rules, "stackup", None) or []
        if not layers_in:
            th = getattr(getattr(rules, "constraints", None), "board_thickness_mm", 1.6)
            return Stackup.default_2layer(dielectric_mm=max(0.2, th - 0.07))
        out: list[StackupLayer] = []
        for ly in layers_in:
            name = getattr(ly, "name", None) or ly.get("name")  # type: ignore[union-attr]
            ltype = getattr(ly, "layer_type", None) or ly.get("layer_type", "other")  # type: ignore[union-attr]
            thick = getattr(ly, "thickness_mm", None)
            if thick is None:
                thick = ly.get("thickness_mm", 0.0)  # type: ignore[union-attr]
            mat = getattr(ly, "material", None) or ly.get("material", "")  # type: ignore[union-attr]
            z0 = getattr(ly, "z0_mm", None)
            if z0 is None:
                z0 = ly.get("z0_mm", 0.0)  # type: ignore[union-attr]
            material = "copper" if ltype == "copper" else (mat or "FR4")
            if ltype in ("core", "prepreg"):
                material = "FR4"
            out.append(
                StackupLayer(
                    name=str(name),
                    thickness_mm=float(thick or 0.0),
                    material=str(material),
                    z0_mm=float(z0 or 0.0),
                )
            )
        return Stackup(layers=out)

    def z_center_mm(self, copper_name: str) -> float:
        for ly in self.layers:
            if ly.name == copper_name:
                return ly.z0_mm + ly.thickness_mm / 2
        # default F.Cu top
        return self.layers[-1].z0_mm + self.layers[-1].thickness_mm / 2 if self.layers else 1.6

    def total_height_mm(self) -> float:
        if not self.layers:
            return 1.6
        top = max(self.layers, key=lambda L: L.z0_mm + L.thickness_mm)
        return top.z0_mm + top.thickness_mm


@dataclass
class CopperPrimitive:
    kind: str  # box | polyline
    layer: str
    net: str = ""
    # box
    cx: float = 0.0
    cy: float = 0.0
    cz: float = 0.0
    w: float = 0.0
    h: float = 0.0
    t: float = 0.035
    # polyline points [(x,y), ...]
    points: list[tuple[float, float]] = field(default_factory=list)
    width_mm: float = 0.25

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "layer": self.layer,
            "net": self.net,
        }
        if self.kind == "box":
            d.update(
                {
                    "cx_mm": self.cx,
                    "cy_mm": self.cy,
                    "cz_mm": self.cz,
                    "w_mm": self.w,
                    "h_mm": self.h,
                    "t_mm": self.t,
                }
            )
        else:
            d.update(
                {
                    "points_mm": [[p[0], p[1]] for p in self.points],
                    "width_mm": self.width_mm,
                    "cz_mm": self.cz,
                    "t_mm": self.t,
                }
            )
        return d


def geometry_from_board(
    board: BoardModel,
    routes: RouteResult | None = None,
    config: PlacementConfig | None = None,
    stackup: Stackup | None = None,
    nets_filter: set[str] | None = None,
) -> list[CopperPrimitive]:
    """Build copper primitives from footprints (pads) and routed segments."""
    stackup = stackup or Stackup.default_2layer()
    cu_t = 0.035
    for ly in stackup.layers:
        if ly.material == "copper":
            cu_t = ly.thickness_mm
            break

    prims: list[CopperPrimitive] = []

    # Pads / bodies as boxes on F.Cu (simplified)
    zf = stackup.z_center_mm("F.Cu")
    for c in board.components.values():
        nets = {p.get("net") for p in c.pads if p.get("net")}
        net = next(iter(nets), "") or ""
        if nets_filter is not None and net and net not in nets_filter:
            continue
        prims.append(
            CopperPrimitive(
                kind="box",
                layer="F.Cu",
                net=str(net),
                cx=c.x_mm,
                cy=c.y_mm,
                cz=zf,
                w=max(c.width_mm, 0.3),
                h=max(c.height_mm, 0.3),
                t=cu_t,
            )
        )

    if routes is None and config is not None:
        routes = clearance_aware_route(board, config, clearance_mm=0.15)
    if routes:
        for s in routes.segments:
            if nets_filter is not None and s.net not in nets_filter:
                continue
            z = stackup.z_center_mm(s.layer if s.layer in ("F.Cu", "B.Cu") else "F.Cu")
            prims.append(
                CopperPrimitive(
                    kind="polyline",
                    layer=s.layer,
                    net=s.net,
                    points=[(s.x1, s.y1), (s.x2, s.y2)],
                    width_mm=s.width_mm,
                    cz=z,
                    t=cu_t,
                )
            )
        for v in routes.vias:
            if nets_filter is not None and v.net not in nets_filter:
                continue
            # via barrel as vertical stack of boxes
            for ly_name in ("F.Cu", "B.Cu"):
                prims.append(
                    CopperPrimitive(
                        kind="box",
                        layer=ly_name,
                        net=v.net,
                        cx=v.x,
                        cy=v.y,
                        cz=stackup.z_center_mm(ly_name),
                        w=v.size_mm,
                        h=v.size_mm,
                        t=cu_t,
                    )
                )
    return prims


# ---------------------------------------------------------------------------
# Minimal Gerber RS-274X parser (lines / flashes of circular+rect apertures)
# ---------------------------------------------------------------------------

@dataclass
class GerberGeometry:
    layer_hint: str
    polylines: list[list[tuple[float, float]]] = field(default_factory=list)
    flashes: list[tuple[float, float, float, float]] = field(default_factory=list)  # x,y,w,h
    unit_mm: bool = True


def parse_gerber(path: str | Path, layer_hint: str = "F.Cu") -> GerberGeometry:
    """Parse a subset of Gerber X for draws (G01 D01) and flashes (D03)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    unit_mm = True
    if "%MOIN*%" in text.upper():
        unit_mm = False

    # apertures: %ADDnnC,0.5*% or %ADDnnR,0.6X0.4*%
    apertures: dict[int, tuple[str, float, float]] = {}
    for m in re.finditer(
        r"%ADD(\d+)([CROP]),([0-9.]+)(?:X([0-9.]+))?\*%",
        text,
        flags=re.IGNORECASE,
    ):
        code = int(m.group(1))
        kind = m.group(2).upper()
        a = float(m.group(3))
        b = float(m.group(4)) if m.group(4) else a
        if not unit_mm:
            a *= 25.4
            b *= 25.4
        apertures[code] = (kind, a, b)

    fmt_x = fmt_y = 4  # decimal digits default 4.6 → 6? use 4.6 style
    # %FSLAX36Y36*% → 3 int 6 dec
    fm = re.search(r"%FS[LTDAI]*X(\d)(\d)Y(\d)(\d)\*%", text, re.I)
    if fm:
        fmt_x = int(fm.group(2))
        fmt_y = int(fm.group(4))

    def decode(num: str, decimals: int) -> float:
        if not num:
            return 0.0
        sign = -1 if num.startswith("-") else 1
        digits = num[1:] if num[0] in "+-" else num
        if not digits:
            return 0.0
        val = sign * int(digits) / (10**decimals)
        return val if unit_mm else val * 25.4

    cur_x = cur_y = 0.0
    cur_ap = None
    poly: list[tuple[float, float]] = []
    geom = GerberGeometry(layer_hint=layer_hint, unit_mm=True)
    # strip macros parameters lines for simplicity — process coordinate ops
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("G04") or line.startswith("%"):
            # aperture select sometimes D10*
            am = re.match(r"D(\d+)\*", line)
            if am and int(am.group(1)) >= 10:
                cur_ap = int(am.group(1))
            continue
        # D-code select mid-line
        dm = re.search(r"D(\d+)\*", line)
        if dm and int(dm.group(1)) >= 10 and "X" not in line and "Y" not in line:
            cur_ap = int(dm.group(1))
            continue

        xm = re.search(r"X(-?\d+)", line)
        ym = re.search(r"Y(-?\d+)", line)
        if xm:
            cur_x = decode(xm.group(1), fmt_x)
        if ym:
            cur_y = decode(ym.group(1), fmt_y)

        if re.search(r"D0?1\*", line) or (line.endswith("D1*") or "*D01*" in line):
            if not poly:
                poly = [(cur_x, cur_y)]
            else:
                poly.append((cur_x, cur_y))
        elif re.search(r"D0?2\*", line) or line.endswith("D2*"):
            if len(poly) >= 2:
                geom.polylines.append(poly)
            poly = [(cur_x, cur_y)]
        elif re.search(r"D0?3\*", line) or line.endswith("D3*"):
            w = h = 0.5
            if cur_ap and cur_ap in apertures:
                kind, a, b = apertures[cur_ap]
                w, h = a, b
            geom.flashes.append((cur_x, cur_y, w, h))
    if len(poly) >= 2:
        geom.polylines.append(poly)
    return geom


def geometry_from_gerbers(
    gerber_paths: dict[str, str | Path],
    stackup: Stackup | None = None,
) -> list[CopperPrimitive]:
    """gerber_paths: layer_name -> file path (e.g. {'F.Cu': 'f_cu.gbr'})."""
    stackup = stackup or Stackup.default_2layer()
    prims: list[CopperPrimitive] = []
    cu_t = 0.035
    for layer_name, path in gerber_paths.items():
        g = parse_gerber(path, layer_hint=layer_name)
        z = stackup.z_center_mm(layer_name if layer_name in ("F.Cu", "B.Cu") else "F.Cu")
        for pl in g.polylines:
            prims.append(
                CopperPrimitive(
                    kind="polyline",
                    layer=layer_name,
                    points=pl,
                    width_mm=0.15,
                    cz=z,
                    t=cu_t,
                )
            )
        for x, y, w, h in g.flashes:
            prims.append(
                CopperPrimitive(
                    kind="box",
                    layer=layer_name,
                    cx=x,
                    cy=y,
                    cz=z,
                    w=w,
                    h=h,
                    t=cu_t,
                )
            )
    return prims


def export_openems_bundle(
    out_dir: str | Path,
    board: BoardModel | None = None,
    routes: RouteResult | None = None,
    config: PlacementConfig | None = None,
    gerber_paths: dict[str, str | Path] | None = None,
    stackup: Stackup | None = None,
    nets_filter: set[str] | None = None,
    f0_hz: float = 1e9,
    fc_hz: float = 1e9,
    design_rules: object | None = None,
) -> dict[str, Path]:
    """Write geometry JSON + openEMS Python script. Returns map of artifact paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if stackup is None and design_rules is not None:
        stackup = Stackup.from_design_rules(design_rules)
    stackup = stackup or Stackup.default_2layer()

    prims: list[CopperPrimitive] = []
    if board is not None:
        prims.extend(
            geometry_from_board(
                board, routes=routes, config=config, stackup=stackup, nets_filter=nets_filter
            )
        )
    if gerber_paths:
        prims.extend(geometry_from_gerbers(gerber_paths, stackup=stackup))

    width = board.width_mm if board else 50.0
    height = board.height_mm if board else 40.0
    if board is None and prims:
        xs = []
        ys = []
        for p in prims:
            if p.kind == "box":
                xs += [p.cx - p.w / 2, p.cx + p.w / 2]
                ys += [p.cy - p.h / 2, p.cy + p.h / 2]
            else:
                for pt in p.points:
                    xs.append(pt[0])
                    ys.append(pt[1])
        if xs:
            width = max(xs) - min(xs) + 2.0
            height = max(ys) - min(ys) + 2.0

    geom_doc = {
        "unit": "mm",
        "board_width_mm": width,
        "board_height_mm": height,
        "stackup": [
            {
                "name": ly.name,
                "thickness_mm": ly.thickness_mm,
                "material": ly.material,
                "z0_mm": ly.z0_mm,
            }
            for ly in stackup.layers
        ],
        "primitives": [p.to_dict() for p in prims],
        "excitation": {"f0_hz": f0_hz, "fc_hz": fc_hz},
    }
    geom_path = out_dir / "board_geometry.json"
    geom_path.write_text(json.dumps(geom_doc, indent=2) + "\n", encoding="utf-8")

    script_path = out_dir / "simulate_board.py"
    script_path.write_text(
        _openems_python_script(width, height, stackup.total_height_mm(), f0_hz, fc_hz),
        encoding="utf-8",
    )

    # Prefer KiCad STEP (tracks + soldermask + silk) when a board path is known
    step_path = None
    if board is not None and board.source_path and Path(board.source_path).exists():
        try:
            from physics_router.kicad_tools import export_step

            step_path = export_step(
                board.source_path,
                out_dir / "board_with_copper.step",
                board_only=True,
                no_components=True,
                include_tracks=True,
                include_pads=True,
                include_zones=True,
                include_inner_copper=True,
                include_silkscreen=True,
                include_soldermask=True,
            )
        except Exception as e:
            step_path = None
            (out_dir / "step_export_error.txt").write_text(str(e), encoding="utf-8")

    readme = out_dir / "OPENEMS_README.txt"
    readme.write_text(
        "OpenEMS export from physics-router\n"
        "==================================\n"
        "1. board_geometry.json — copper primitives in mm (boxes + polylines).\n"
        "2. simulate_board.py — loads JSON and builds a CSXCAD model.\n"
        "3. board_with_copper.step — KiCad STEP with tracks, pads, zones,\n"
        "   inner copper, soldermask, and silkscreen (when kicad-cli available).\n"
        "   Prefer STEP for accurate 3D geometry in FreeCAD → mesh → OpenEMS.\n"
        "\n"
        "Requirements: openEMS + Python CSXCAD bindings; KiCad for STEP.\n"
        "  https://docs.openems.de/\n"
        "\n"
        "Run:\n"
        "  python simulate_board.py\n"
        "\n"
        "Or: physics-router export-step --pcb board.kicad_pcb --out-dir sim_geo\n"
        "\n"
        "The script maps copper as PEC metal and FR4 dielectric from stackup.\n"
        "Edit ports/excitation for your SI or EMI experiment.\n",
        encoding="utf-8",
    )
    out: dict[str, Path] = {
        "geometry": geom_path,
        "script": script_path,
        "readme": readme,
    }
    if step_path is not None:
        out["step"] = step_path
    return out


def _openems_python_script(
    width_mm: float,
    height_mm: float,
    height_z_mm: float,
    f0: float,
    fc: float,
) -> str:
    # Script loads sibling board_geometry.json so it stays self-contained.
    return f'''#!/usr/bin/env python3
"""Auto-generated openEMS/CSXCAD driver by physics-router."""
from __future__ import annotations

import json
from pathlib import Path

# mm -> m
MM = 1e-3

def main() -> None:
    here = Path(__file__).resolve().parent
    geom = json.loads((here / "board_geometry.json").read_text(encoding="utf-8"))

    try:
        from CSXCAD import ContinuousStructure
        from openEMS import openEMS
        from openEMS.physical_constants import C0
    except ImportError as e:
        raise SystemExit(
            "CSXCAD/openEMS Python bindings not installed.\\n"
            "Install openEMS with Python support, then re-run.\\n"
            f"Original error: {{e}}"
        )

    unit = MM
    CSX = ContinuousStructure()
    mesh = CSX.GetGrid()
    mesh.SetDeltaUnit(unit)

    # Airbox around board
    margin = 5.0  # mm
    w = float(geom["board_width_mm"])
    h = float(geom["board_height_mm"])
    zmax = {height_z_mm:.4f} + 2.0

    mesh.AddLine("x", [-margin, w + margin])
    mesh.AddLine("y", [-margin, h + margin])
    mesh.AddLine("z", [-1.0, zmax])

    # Dielectric slab (FR4)
    sub = CSX.AddMaterial("FR4", epsilon=4.4)
    # Find dielectric thickness from stackup
    diel_t = 1.6
    diel_z0 = 0.035
    for ly in geom.get("stackup", []):
        if ly.get("material") == "FR4":
            diel_t = ly["thickness_mm"]
            diel_z0 = ly["z0_mm"]
    sub.AddBox([0, 0, diel_z0], [w, h, diel_z0 + diel_t])

    metal = CSX.AddMetal("copper")
    for prim in geom.get("primitives", []):
        t = prim.get("t_mm", 0.035)
        if prim["kind"] == "box":
            cx, cy, cz = prim["cx_mm"], prim["cy_mm"], prim["cz_mm"]
            hw, hh = prim["w_mm"] / 2, prim["h_mm"] / 2
            metal.AddBox(
                [cx - hw, cy - hh, cz - t / 2],
                [cx + hw, cy + hh, cz + t / 2],
            )
        elif prim["kind"] == "polyline":
            pts = prim.get("points_mm") or []
            width = prim.get("width_mm", 0.25)
            cz = prim.get("cz_mm", diel_z0 + diel_t)
            for i in range(len(pts) - 1):
                x1, y1 = pts[i]
                x2, y2 = pts[i + 1]
                # approximate trace as thin box along segment AABB + width
                xmin, xmax = min(x1, x2) - width / 2, max(x1, x2) + width / 2
                ymin, ymax = min(y1, y2) - width / 2, max(y1, y2) + width / 2
                metal.AddBox([xmin, ymin, cz - t / 2], [xmax, ymax, cz + t / 2])

    # Smooth mesh
    mesh.SmoothMeshLines("all", 0.5, 1.4)

    FDTD = openEMS(NrTS=5e5, EndCriteria=1e-4)
    FDTD.SetGaussExcite({f0!r}, {fc!r})
    FDTD.SetBoundaryCond(["MUR", "MUR", "MUR", "MUR", "MUR", "MUR"])
    FDTD.SetCSX(CSX)

    sim_path = str(here / "openems_out")
    Path(sim_path).mkdir(exist_ok=True)
    FDTD.Run(sim_path, verbose=3, cleanup=True)
    print("openEMS finished:", sim_path)

if __name__ == "__main__":
    main()
'''


def segments_to_result(segments: list[RouteSegment]) -> RouteResult:
    r = RouteResult(segments=list(segments))
    r.total_length_mm = sum(
        math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in segments
    )
    return r
