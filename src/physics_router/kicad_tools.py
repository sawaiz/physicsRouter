"""KiCad integration: locate tools, run DRC, render boards via kicad-cli / pcbnew.

Prefer official KiCad outputs for documentation and validation:
- ``kicad-cli pcb drc`` — design rule check JSON/report
- ``kicad-cli pcb export svg`` — 2D layer plots (pcbnew plot engine)
- ``kicad-cli pcb render`` — 3D board render to PNG
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
) -> DrcReport:
    """Run KiCad DRC via kicad-cli; return structured report."""
    cli = find_kicad_cli()
    if cli is None:
        raise FileNotFoundError(
            "kicad-cli not found. Install KiCad or set KICAD_CLI to the binary path."
        )
    pcb_path = Path(pcb_path).resolve()
    if out_json is None:
        out_json = pcb_path.with_suffix(".drc.json")
    out_json = Path(out_json)

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
    report.unconnected = list(data.get("unconnected_items") or [])
    return report


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
