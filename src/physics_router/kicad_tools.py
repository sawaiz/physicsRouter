"""KiCad integration: locate tools, run DRC, render boards via kicad-cli / pcbnew.

Prefer official KiCad outputs for documentation and validation:
- ``kicad-cli pcb drc`` — design rule check JSON/report
- ``kicad-cli pcb export svg`` — 2D layer plots (pcbnew plot engine)
- ``kicad-cli pcb render`` — 3D board render to PNG
- ``kicad-cli pcb export step`` — 3D STEP with tracks/pads/soldermask/silkscreen
- Optional direct ``pcbnew`` (KiCad-bundled Python) PLOT_CONTROLLER for SVG/PDF

Environment overrides:
- ``KICAD_CLI`` — path to kicad-cli
- ``KICAD_PYTHON`` — path to KiCad's python3 that has ``import pcbnew``
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def find_kicad_cli() -> Path | None:
    env = os.environ.get("KICAD_CLI")
    if env and Path(env).is_file():
        return Path(env)
    which = shutil.which("kicad-cli")
    if which:
        return Path(which)
    candidates = [
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli.sh",
        "/usr/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
        "C:/Program Files/KiCad/9.0/bin/kicad-cli.exe",
        "C:/Program Files/KiCad/8.0/bin/kicad-cli.exe",
        "C:/Program Files/KiCad/10.0/bin/kicad-cli.exe",
    ]
    for c in candidates:
        if Path(c).is_file():
            return Path(c)
    return None


def find_kicad_python() -> Path | None:
    env = os.environ.get("KICAD_PYTHON")
    if env and Path(env).is_file():
        return Path(env)
    candidates = [
        "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9",
        "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
        "/usr/bin/python3",
    ]
    for c in candidates:
        p = Path(c)
        if not p.is_file():
            continue
        try:
            r = subprocess.run(
                [str(p), "-c", "import pcbnew; print(pcbnew.GetBuildVersion())"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if r.returncode == 0 and r.stdout.strip():
                return p
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


@dataclass
class DrcViolation:
    type: str
    severity: str
    description: str
    items: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "severity": self.severity,
            "description": self.description,
            "items": self.items,
        }


@dataclass
class DrcReport:
    source: str
    kicad_version: str = ""
    violations: list[DrcViolation] = field(default_factory=list)
    unconnected: list[dict[str, Any]] = field(default_factory=list)
    raw_path: str | None = None
    exit_code: int = 0
    error_counts: dict[str, int] = field(default_factory=dict)

    @property
    def error_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "warning")

    @property
    def passed(self) -> bool:
        return self.error_count == 0

    def copper_related(self) -> list[DrcViolation]:
        copper_types = {
            "clearance",
            "track_width",
            "tracks_crossing",
            "shorting_items",
            "via_dangling",
            "track_dangling",
            "copper_edge_clearance",
            "hole_clearance",
            "hole_to_hole",
            "holes_co_located",
            "annular_width",
            "drill_out_of_range",
            "too_many_vias",
            "zones_intersect",
            "starved_thermal",
            "connection_width",
        }
        return [v for v in self.violations if v.type in copper_types or "track" in v.type or "via" in v.type or "clearance" in v.type]

    def to_dict(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        for v in self.violations:
            by_type[v.type] = by_type.get(v.type, 0) + 1
        copper = self.copper_related()
        return {
            "source": self.source,
            "kicad_version": self.kicad_version,
            "passed": self.passed,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "unconnected_count": len(self.unconnected),
            "violation_count": len(self.violations),
            "copper_violation_count": len(copper),
            "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
            "copper_sample": [v.to_dict() for v in copper[:15]],
            "raw_path": self.raw_path,
            "exit_code": self.exit_code,
        }


def run_drc(
    pcb_path: str | Path,
    out_json: str | Path | None = None,
    *,
    severity_all: bool = True,
    refill_zones: bool = False,
    schematic_parity: bool = False,
    timeout_s: float = 300,
    all_track_errors: bool = True,
) -> DrcReport:
    """Run official KiCad DRC engine via ``kicad-cli pcb drc``.

    This is the real KiCad DRC (same engine as the PCB editor), not a reimplementation.
    We intentionally do **not** vendor GPL KiCad sources; calling kicad-cli keeps
    licensing clean and always matches the installed KiCad version.
    """
    cli = find_kicad_cli()
    if cli is None:
        raise FileNotFoundError(
            "kicad-cli not found. Install KiCad or set KICAD_CLI to the binary path."
        )
    pcb_path = Path(pcb_path).resolve()
    if out_json is None:
        out_json = pcb_path.with_suffix(".drc.json")
    out_json = Path(out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(cli),
        "pcb",
        "drc",
        "--format",
        "json",
        "--units",
        "mm",
        "-o",
        str(out_json),
    ]
    if severity_all:
        cmd.append("--severity-all")
    if refill_zones:
        cmd.append("--refill-zones")
    if schematic_parity:
        cmd.append("--schematic-parity")
    if all_track_errors:
        cmd.append("--all-track-errors")
    cmd.append(str(pcb_path))

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    report = DrcReport(source=str(pcb_path), exit_code=proc.returncode, raw_path=str(out_json))
    if not out_json.exists():
        report.violations.append(
            DrcViolation(
                type="tool_error",
                severity="error",
                description=f"DRC produced no report. stderr={proc.stderr[:500]}",
            )
        )
        return report

    data = json.loads(out_json.read_text(encoding="utf-8"))
    report.kicad_version = str(data.get("kicad_version", ""))
    for v in data.get("violations") or []:
        report.violations.append(
            DrcViolation(
                type=str(v.get("type", "unknown")),
                severity=str(v.get("severity", "error")),
                description=str(v.get("description", "")),
                items=list(v.get("items") or []),
            )
        )
    report.unconnected = list(
        data.get("unconnected_items") or data.get("unconnected") or []
    )
    return report


def kicad_drc_route(
    source_pcb: str | Path,
    route: Any,
    *,
    work_dir: str | Path | None = None,
    timeout_s: float = 180,
    keep_files: bool = False,
) -> dict[str, Any]:
    """Write ``route`` copper onto a copy of ``source_pcb`` and run KiCad DRC.

    Returns a dict compatible with router quality / improve snapshots::

        {
          "available": True,
          "passed": bool,           # zero error-level violations
          "copper_passed": bool,    # zero copper-class violations
          "error_count": int,
          "copper_violation_count": int,
          "unconnected_count": int,
          "by_type": {...},
          "samples": [...],
          "pcb_path": str,
          "report_path": str | None,
          "kicad_version": str,
        }

    Optimized for the improve loop: one temp PCB + one kicad-cli invocation.
    """
    from physics_router.router import append_routes_to_kicad_pcb

    source_pcb = Path(source_pcb).resolve()
    if not source_pcb.is_file():
        return {
            "available": False,
            "error": f"PCB not found: {source_pcb}",
            "passed": False,
            "copper_passed": False,
            "error_count": 0,
            "copper_violation_count": 0,
            "unconnected_count": 0,
        }
    if find_kicad_cli() is None:
        return {
            "available": False,
            "error": "kicad-cli not found",
            "passed": False,
            "copper_passed": False,
            "error_count": 0,
            "copper_violation_count": 0,
            "unconnected_count": 0,
        }

    if work_dir is None:
        tmp = tempfile.mkdtemp(prefix="pr_kicad_drc_")
        work = Path(tmp)
        cleanup = not keep_files
    else:
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        cleanup = False

    try:
        routed = work / "routed.kicad_pcb"
        report_json = work / "drc.json"
        # KiCad resolves board/netclass constraints from a same-basename
        # project file. Without this copy, temp-route DRC silently used KiCad's
        # generic 0.20 mm defaults instead of the source project's 0.127 mm
        # HALO rules, inflating both track-width and clearance counts.
        source_project = source_pcb.with_suffix(".kicad_pro")
        if source_project.is_file():
            shutil.copy2(source_project, work / "routed.kicad_pro")
        source_rules = source_pcb.with_suffix(".kicad_dru")
        if source_rules.is_file():
            shutil.copy2(source_rules, work / "routed.kicad_dru")
        # Clear pre-existing tracks so DRC measures autorouter copper only
        append_routes_to_kicad_pcb(
            str(source_pcb),
            str(routed),
            route,
            replace_previous=True,
            clear_existing_copper=True,
        )
        rep = run_drc(
            routed,
            report_json,
            severity_all=True,
            all_track_errors=True,
            timeout_s=timeout_s,
        )
        d = rep.to_dict()
        # Copper / connectivity classes that autorouter must fix (ignore silk/lib noise)
        hard_types = {
            "shorting_items",
            "tracks_crossing",
            "clearance",
            "copper_edge_clearance",
            "hole_clearance",
            "hole_to_hole",
            "holes_co_located",
            "track_width",
            "via_dangling",
            "track_dangling",
            "annular_width",
            "drill_out_of_range",
            "zones_intersect",
            "connection_width",
            "starved_thermal",
        }
        def involves_generated_route(violation: DrcViolation) -> bool:
            # Existing board pads/graphics can already violate DRC. All tracks,
            # vias and zones were cleared before appending this candidate, so a
            # generated-route object in the item pair cleanly attributes the
            # violation to the autorouter without subtracting aggregate counts.
            route_prefixes = ("Track ", "Via ", "Zone ", "Filled area ")
            return any(
                str(item.get("description") or "").startswith(route_prefixes)
                for item in violation.items
            )

        copper_items = [
            v
            for v in rep.violations
            if involves_generated_route(v)
            and (
                v.type in hard_types
                or "short" in v.type
                or "track" in v.type
                or "via" in v.type
                or v.type == "clearance"
            )
        ]
        copper_errors = [v for v in copper_items if v.severity == "error"]
        copper_n = len(copper_items)
        copper_err_n = len(copper_errors)
        samples = []
        for v in copper_errors[:12] or copper_items[:12]:
            samples.append(f"{v.severity}:{v.type}: {v.description[:120]}")
        return {
            "available": True,
            "passed": bool(rep.passed),
            # Copper pass ignores silk/text/lib footprint issues on the donor board
            "copper_passed": copper_err_n == 0,
            "error_count": rep.error_count,
            "copper_error_count": copper_err_n,
            "warning_count": rep.warning_count,
            "copper_violation_count": copper_n,
            "unconnected_count": len(rep.unconnected),
            "violation_count": len(rep.violations),
            "by_type": d.get("by_type") or {},
            "samples": samples,
            "pcb_path": str(routed),
            "report_path": str(report_json) if report_json.exists() else None,
            "kicad_version": rep.kicad_version,
            "exit_code": rep.exit_code,
        }
    except Exception as e:
        return {
            "available": False,
            "error": f"{type(e).__name__}: {e}",
            "passed": False,
            "copper_passed": False,
            "error_count": 0,
            "copper_violation_count": 0,
            "unconnected_count": 0,
        }
    finally:
        if cleanup:
            try:
                shutil.rmtree(work, ignore_errors=True)
            except Exception:
                pass


def export_svg_layers(
    pcb_path: str | Path,
    out_dir: str | Path,
    layers: list[str] | None = None,
    *,
    mode_multi: bool = True,
    fit_board: bool = True,
    theme: str = "",
    timeout_s: float = 180,
) -> list[Path]:
    """Plot board layers to SVG using kicad-cli (pcbnew plot backend)."""
    cli = find_kicad_cli()
    if cli is None:
        raise FileNotFoundError("kicad-cli not found")
    pcb_path = Path(pcb_path).resolve()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if layers is None:
        layers = ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "F.SilkS", "Edge.Cuts", "F.Mask"]

    cmd = [
        str(cli),
        "pcb",
        "export",
        "svg",
        "--output",
        str(out_dir),
        "--layers",
        ",".join(layers),
        "--exclude-drawing-sheet",
        "--drill-shape-opt",
        "2",
    ]
    if mode_multi:
        cmd.append("--mode-multi")
    else:
        cmd.append("--mode-single")
    if fit_board:
        cmd.append("--fit-page-to-board")
    if theme:
        cmd.extend(["--theme", theme])
    cmd.append(str(pcb_path))

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        raise RuntimeError(f"SVG export failed: {proc.stderr or proc.stdout}")

    return sorted(out_dir.glob("*.svg"))


def render_3d(
    pcb_path: str | Path,
    out_png: str | Path,
    *,
    side: str = "top",
    width: int = 1600,
    height: int = 1200,
    quality: str = "high",
    rotate: str = "",
    floor: bool = True,
    timeout_s: float = 300,
) -> Path:
    """3D board render via kicad-cli pcb render (official 3D canvas)."""
    cli = find_kicad_cli()
    if cli is None:
        raise FileNotFoundError("kicad-cli not found")
    pcb_path = Path(pcb_path).resolve()
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)

    def _run(q: str, use_floor: bool) -> subprocess.CompletedProcess[str]:
        cmd = [
            str(cli),
            "pcb",
            "render",
            "-o",
            str(out_png),
            "--side",
            side,
            "--width",
            str(width),
            "--height",
            str(height),
            "--quality",
            q,
            "--background",
            "opaque",
        ]
        if use_floor:
            cmd.append("--floor")
        if rotate:
            cmd.extend(["--rotate", rotate])
        cmd.append(str(pcb_path))
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)

    # KiCad 10: some quality/floor combos throw; fall back gracefully
    attempts = [(quality, floor), (quality, False), ("basic", False), ("high", False)]
    last_err = ""
    for q, use_floor in attempts:
        proc = _run(q, use_floor)
        if proc.returncode == 0 and out_png.exists() and out_png.stat().st_size > 0:
            return out_png
        last_err = proc.stderr or proc.stdout
    raise RuntimeError(f"3D render failed: {last_err}")


def plot_with_pcbnew(
    pcb_path: str | Path,
    out_dir: str | Path,
    layers: list[str] | None = None,
) -> list[Path]:
    """Plot copper layers using KiCad-bundled pcbnew PLOT_CONTROLLER.

    Spawns KiCad's Python so ``import pcbnew`` works without matching system Python.
    """
    kpy = find_kicad_python()
    if kpy is None:
        raise FileNotFoundError(
            "KiCad Python with pcbnew not found. Set KICAD_PYTHON or install KiCad."
        )
    pcb_path = Path(pcb_path).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    layers = layers or ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "Edge.Cuts"]

    # Inline script for KiCad-bundled Python (API varies 7–10)
    script = f'''
import sys
from pathlib import Path
import pcbnew

pcb_path = r"{pcb_path}"
out_dir = Path(r"{out_dir}")
layer_names = {layers!r}

board = pcbnew.LoadBoard(pcb_path)
pc = pcbnew.PLOT_CONTROLLER(board)
po = pc.GetPlotOptions()
po.SetOutputDirectory(str(out_dir))
po.SetPlotFrameRef(False)
for meth, args in [
    ("SetAutoScale", (True,)),
    ("SetScale", (0,)),
    ("SetMirror", (False,)),
    ("SetUseGerberAttributes", (False,)),
    ("SetSubtractMaskFromSilk", (False,)),
]:
    fn = getattr(po, meth, None)
    if callable(fn):
        try:
            fn(*args)
        except Exception:
            pass
try:
    po.SetDrillMarksType(pcbnew.DRILL_MARKS_FULL_DRILL_SHAPE)
except Exception:
    try:
        po.SetDrillMarksType(2)
    except Exception:
        pass

for name in layer_names:
    try:
        layer_id = board.GetLayerID(name)
    except Exception:
        continue
    try:
        if int(layer_id) < 0:
            continue
    except Exception:
        pass
    safe = name.replace(".", "_")
    try:
        pc.SetLayer(layer_id)
        pc.OpenPlotfile(safe, pcbnew.PLOT_FORMAT_SVG, name)
        pc.PlotLayer()
        pc.ClosePlot()
    except Exception as e:
        sys.stderr.write(f"skip {{name}}: {{e}}\\n")
print("OK")
'''
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(script)
        script_path = f.name
    try:
        proc = subprocess.run(
            [str(kpy), script_path],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(out_dir),
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"pcbnew plot failed: {proc.stderr or proc.stdout}\n"
                f"(kicad python={kpy})"
            )
    finally:
        Path(script_path).unlink(missing_ok=True)

    return sorted(out_dir.glob("*.svg"))


def _pcb_export_3d_cmd(
    fmt: str,
    pcb_path: Path,
    out_path: Path,
    *,
    include_tracks: bool = True,
    include_pads: bool = True,
    include_zones: bool = True,
    include_inner_copper: bool = True,
    include_silkscreen: bool = True,
    include_soldermask: bool = True,
    board_only: bool = False,
    no_components: bool = False,
    no_board_body: bool = False,
    subst_models: bool = True,
    fuse_shapes: bool = False,
    force: bool = True,
    net_filter: str = "",
    component_filter: str = "",
) -> list[str]:
    """Build kicad-cli ``pcb export step|glb`` argv with full visual layers."""
    cli = find_kicad_cli()
    if cli is None:
        raise FileNotFoundError("kicad-cli not found")
    cmd = [str(cli), "pcb", "export", fmt, "-o", str(out_path)]
    if force:
        cmd.append("--force")
    if board_only:
        cmd.append("--board-only")
    if no_components:
        cmd.append("--no-components")
    if no_board_body:
        cmd.append("--no-board-body")
    if subst_models:
        cmd.append("--subst-models")
    if include_tracks:
        cmd.append("--include-tracks")
    if include_pads:
        cmd.append("--include-pads")
    if include_zones:
        cmd.append("--include-zones")
    if include_inner_copper:
        cmd.append("--include-inner-copper")
    if include_silkscreen:
        cmd.append("--include-silkscreen")
    if include_soldermask:
        cmd.append("--include-soldermask")
    if fuse_shapes:
        cmd.append("--fuse-shapes")
    if net_filter:
        cmd.extend(["--net-filter", net_filter])
    if component_filter:
        cmd.extend(["--component-filter", component_filter])
    cmd.append(str(pcb_path))
    return cmd


def export_step(
    pcb_path: str | Path,
    out_step: str | Path,
    *,
    include_tracks: bool = True,
    include_pads: bool = True,
    include_zones: bool = True,
    include_inner_copper: bool = True,
    include_silkscreen: bool = True,
    include_soldermask: bool = True,
    board_only: bool = False,
    no_components: bool = False,
    fuse_shapes: bool = False,
    force: bool = True,
    net_filter: str = "",
    subst_models: bool = True,
    timeout_s: float = 600,
) -> Path:
    """Export STEP with copper + soldermask + silkscreen (+ component STEP models).

    Uses footprint 3D models (``.step`` / ``.stp`` under the project) via
    ``--subst-models`` so LEDs, MCU, battery, etc. match the library files.
    """
    pcb_path = Path(pcb_path).resolve()
    out_step = Path(out_step)
    out_step.parent.mkdir(parents=True, exist_ok=True)
    cmd = _pcb_export_3d_cmd(
        "step",
        pcb_path,
        out_step,
        include_tracks=include_tracks,
        include_pads=include_pads,
        include_zones=include_zones,
        include_inner_copper=include_inner_copper,
        include_silkscreen=include_silkscreen,
        include_soldermask=include_soldermask,
        board_only=board_only,
        no_components=no_components,
        subst_models=subst_models,
        fuse_shapes=fuse_shapes,
        force=force,
        net_filter=net_filter,
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0 or not out_step.exists():
        raise RuntimeError(f"STEP export failed: {proc.stderr or proc.stdout}")
    return out_step


def export_glb(
    pcb_path: str | Path,
    out_glb: str | Path,
    *,
    include_tracks: bool = True,
    include_pads: bool = True,
    include_zones: bool = True,
    include_inner_copper: bool = True,
    include_silkscreen: bool = True,
    include_soldermask: bool = True,
    board_only: bool = False,
    no_components: bool = False,
    subst_models: bool = True,
    fuse_shapes: bool = False,
    force: bool = True,
    net_filter: str = "",
    timeout_s: float = 600,
) -> Path:
    """Export binary glTF (GLB) for three.js: components + mask + silk + copper.

    Preferred viewer path — browser loads this with ``GLTFLoader``. Component
    geometry comes from project STEP models (``--subst-models``).
    """
    pcb_path = Path(pcb_path).resolve()
    out_glb = Path(out_glb)
    out_glb.parent.mkdir(parents=True, exist_ok=True)
    cmd = _pcb_export_3d_cmd(
        "glb",
        pcb_path,
        out_glb,
        include_tracks=include_tracks,
        include_pads=include_pads,
        include_zones=include_zones,
        include_inner_copper=include_inner_copper,
        include_silkscreen=include_silkscreen,
        include_soldermask=include_soldermask,
        board_only=board_only,
        no_components=no_components,
        subst_models=subst_models,
        fuse_shapes=fuse_shapes,
        force=force,
        net_filter=net_filter,
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0 or not out_glb.exists():
        raise RuntimeError(f"GLB export failed: {proc.stderr or proc.stdout}")
    return out_glb


def export_board_visual_3d(
    pcb_path: str | Path,
    out_dir: str | Path,
    *,
    stem: str | None = None,
    also_step: bool = True,
) -> dict[str, Any]:
    """Export full visual board: GLB (viewer) + optional STEP (CAD/sim).

    Includes footprint STEP models, tracks, pads, zones, inner copper,
    soldermask, and silkscreen.
    """
    pcb_path = Path(pcb_path).resolve()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or pcb_path.stem
    glb = export_glb(
        pcb_path,
        out_dir / f"{stem}.glb",
        board_only=False,
        no_components=False,
        include_tracks=True,
        include_pads=True,
        include_zones=True,
        include_inner_copper=True,
        include_silkscreen=True,
        include_soldermask=True,
        subst_models=True,
    )
    result: dict[str, Any] = {
        "pcb": str(pcb_path),
        "glb": str(glb),
        "glb_bytes": glb.stat().st_size,
        "includes": [
            "component STEP/STP models (--subst-models)",
            "tracks/vias",
            "pads",
            "zones",
            "inner copper",
            "silkscreen",
            "soldermask",
            "board body",
        ],
    }
    if also_step:
        step = export_step(
            pcb_path,
            out_dir / f"{stem}_full.step",
            board_only=False,
            no_components=False,
            include_tracks=True,
            include_pads=True,
            include_zones=True,
            include_inner_copper=True,
            include_silkscreen=True,
            include_soldermask=True,
            subst_models=True,
        )
        result["step"] = str(step)
        result["step_bytes"] = step.stat().st_size
    return result


def export_simulation_bundle(
    pcb_path: str | Path,
    out_dir: str | Path,
    *,
    nets_filter: str = "",
    board_only: bool = True,
) -> dict[str, Any]:
    """Export STEP (+ optional net-filtered STEP) for OpenEMS / FEM pipelines."""
    pcb_path = Path(pcb_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    full = export_step(
        pcb_path,
        out_dir / f"{pcb_path.stem}_full.step",
        board_only=board_only,
        no_components=board_only,
        include_tracks=True,
        include_pads=True,
        include_zones=True,
        include_inner_copper=True,
        include_silkscreen=True,
        include_soldermask=True,
    )
    result: dict[str, Any] = {
        "pcb": str(pcb_path),
        "step_full": str(full),
        "size_bytes": full.stat().st_size,
        "notes": [
            "STEP includes tracks, pads, zones, inner copper, soldermask, silkscreen",
            "Import into FreeCAD/CadQuery/OpenEMS converters as needed",
        ],
    }
    if nets_filter:
        filtered = export_step(
            pcb_path,
            out_dir / f"{pcb_path.stem}_nets.step",
            board_only=True,
            no_components=True,
            include_tracks=True,
            include_pads=True,
            include_zones=True,
            include_inner_copper=True,
            include_silkscreen=False,
            include_soldermask=True,
            net_filter=nets_filter,
        )
        result["step_nets"] = str(filtered)
        result["net_filter"] = nets_filter
    (out_dir / "step_manifest.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


def run_erc(
    sch_path: str | Path,
    out_json: str | Path | None = None,
    *,
    severity_all: bool = True,
    timeout_s: float = 300,
) -> dict[str, Any]:
    """Run official schematic ERC via kicad-cli; return a compact summary dict."""
    cli = find_kicad_cli()
    if cli is None:
        raise FileNotFoundError("kicad-cli not found")
    sch_path = Path(sch_path).resolve()
    if out_json is None:
        out_json = sch_path.with_suffix(".erc.json")
    out_json = Path(out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(cli),
        "sch",
        "erc",
        "--format",
        "json",
        "--units",
        "mm",
        "-o",
        str(out_json),
    ]
    if severity_all:
        cmd.append("--severity-all")
    cmd.append(str(sch_path))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    summary: dict[str, Any] = {
        "source": str(sch_path),
        "raw_path": str(out_json),
        "exit_code": proc.returncode,
        "error_count": 0,
        "warning_count": 0,
        "violation_count": 0,
        "by_type": {},
        "samples": [],
    }
    if not out_json.exists():
        summary["error"] = (proc.stderr or proc.stdout or "no ERC output")[:800]
        return summary
    try:
        data = json.loads(out_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        summary["error"] = f"ERC JSON parse failed: {e}"
        return summary
    violations = data.get("violations") or data.get("errors") or []
    by_type: dict[str, int] = {}
    for v in violations:
        sev = str(v.get("severity", "error")).lower()
        typ = str(v.get("type", v.get("description", "unknown")))[:80]
        by_type[typ] = by_type.get(typ, 0) + 1
        if sev == "warning":
            summary["warning_count"] += 1
        else:
            summary["error_count"] += 1
        if len(summary["samples"]) < 12:
            summary["samples"].append(
                {
                    "severity": sev,
                    "type": typ,
                    "description": str(v.get("description", ""))[:200],
                }
            )
    summary["violation_count"] = len(violations)
    summary["by_type"] = dict(sorted(by_type.items(), key=lambda x: -x[1])[:20])
    summary["passed"] = summary["error_count"] == 0
    return summary


def find_schematic_for_pcb(pcb_path: str | Path) -> Path | None:
    """Best-effort locate a root schematic next to a PCB."""
    pcb_path = Path(pcb_path)
    stem = pcb_path.stem
    candidates = [
        pcb_path.with_suffix(".kicad_sch"),
        pcb_path.parent / f"{stem}.kicad_sch",
        pcb_path.parent.parent / f"{stem}.kicad_sch",
    ]
    for c in candidates:
        if c.is_file():
            return c
    # any schematic in same folder
    schs = list(pcb_path.parent.glob("*.kicad_sch"))
    return schs[0] if schs else None


def validate_copper_board(
    pcb_path: str | Path,
    out_dir: str | Path | None = None,
    *,
    max_copper_errors: int | None = None,
) -> dict[str, Any]:
    """Run DRC and summarize copper-related failures for a board file."""
    pcb_path = Path(pcb_path)
    out_dir = Path(out_dir) if out_dir else pcb_path.parent / "drc_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = run_drc(pcb_path, out_dir / "drc.json")
    summary = report.to_dict()
    summary["tool"] = {
        "kicad_cli": str(find_kicad_cli()),
        "kicad_python": str(find_kicad_python()),
        "platform": platform.platform(),
    }
    if max_copper_errors is not None:
        summary["copper_ok"] = summary["copper_violation_count"] <= max_copper_errors
    else:
        summary["copper_ok"] = summary["error_count"] == 0
    (out_dir / "drc_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def render_board_suite(
    pcb_path: str | Path,
    out_dir: str | Path,
    *,
    use_pcbnew: bool = True,
    layers: list[str] | None = None,
) -> dict[str, Any]:
    """Produce official KiCad 2D SVG plots + 3D PNG renders."""
    pcb_path = Path(pcb_path).resolve()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_cli_dir = out_dir / "svg_cli"
    svg_pcbnew_dir = out_dir / "svg_pcbnew"
    render_dir = out_dir / "render3d"
    render_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {"pcb": str(pcb_path), "outputs": {}}

    # CLI SVG (always preferred for docs — stable)
    try:
        svgs = export_svg_layers(pcb_path, svg_cli_dir, layers=layers)
        result["outputs"]["svg_cli"] = [str(p) for p in svgs]
    except Exception as e:
        result["outputs"]["svg_cli_error"] = str(e)

    # pcbnew PLOT_CONTROLLER
    if use_pcbnew:
        try:
            svgs = plot_with_pcbnew(pcb_path, svg_pcbnew_dir, layers=layers)
            result["outputs"]["svg_pcbnew"] = [str(p) for p in svgs]
        except Exception as e:
            result["outputs"]["svg_pcbnew_error"] = str(e)

    # 3D renders
    for side, rot, name in [
        ("top", "", "top.png"),
        ("bottom", "", "bottom.png"),
        ("top", "-45,0,45", "isometric.png"),
    ]:
        try:
            p = render_3d(
                pcb_path,
                render_dir / name,
                side=side if not rot else "top",
                rotate=rot,
                quality="high",
            )
            result["outputs"].setdefault("render3d", []).append(str(p))
        except Exception as e:
            result["outputs"][f"render3d_{name}_error"] = str(e)

    (out_dir / "render_manifest.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result
