#!/usr/bin/env python3
"""Run KiCad DRC + official pcbnew/kicad-cli renders for docs.

Requires KiCad installed (kicad-cli on PATH or standard macOS app location).

  python scripts/generate_kicad_renders.py
  python scripts/generate_kicad_renders.py --pcb path/to/board.kicad_pcb
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from physics_router.kicad_tools import (
    find_kicad_cli,
    find_kicad_python,
    render_board_suite,
    validate_copper_board,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
DOCS = ROOT / "docs" / "images" / "kicad"
OUT = ROOT / "examples" / "halo-90" / "kicad_validation"


def svg_to_png(svg: Path, png: Path, width: int = 1400) -> bool:
    """Rasterize SVG if rsvg-convert, inkscape, or qlmanage is available."""
    png.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsvg-convert"):
        subprocess.run(
            ["rsvg-convert", "-w", str(width), "-o", str(png), str(svg)],
            check=False,
        )
        return png.exists()
    if shutil.which("inkscape"):
        subprocess.run(
            [
                "inkscape",
                str(svg),
                f"--export-filename={png}",
                f"--export-width={width}",
            ],
            check=False,
        )
        return png.exists()
    # macOS Quick Look
    if shutil.which("qlmanage"):
        tmp = png.parent / "ql"
        tmp.mkdir(exist_ok=True)
        subprocess.run(
            ["qlmanage", "-t", "-s", str(width), "-o", str(tmp), str(svg)],
            capture_output=True,
            check=False,
        )
        # qlmanage names file as <name>.svg.png
        cand = tmp / f"{svg.name}.png"
        if cand.exists():
            shutil.copy(cand, png)
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pcb", type=Path, default=DEFAULT_PCB)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    pcb = args.pcb
    if not pcb.exists():
        raise SystemExit(f"PCB not found: {pcb} (clone halo-90 or pass --pcb)")

    print("kicad-cli:", find_kicad_cli())
    print("kicad python/pcbnew:", find_kicad_python())

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)

    # DRC
    print("Running KiCad DRC…")
    summary = validate_copper_board(pcb, out / "drc")
    (out / "drc_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"  errors={summary['error_count']} warnings={summary['warning_count']} "
        f"copper={summary['copper_violation_count']} passed={summary['passed']}"
    )

    # Renders
    print("Rendering with kicad-cli / pcbnew…")
    layers = ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu", "Edge.Cuts", "F.SilkS"]
    manifest = render_board_suite(pcb, out / "renders", use_pcbnew=True, layers=layers)
    (out / "render_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    # Copy key assets into docs/images/kicad for README
    copied = []
    render3d = manifest.get("outputs", {}).get("render3d") or []
    for src in render3d:
        p = Path(src)
        if p.exists():
            dest = DOCS / f"kicad_3d_{p.name}"
            shutil.copy(p, dest)
            copied.append(str(dest.relative_to(ROOT)))

    svg_cli = manifest.get("outputs", {}).get("svg_cli") or []
    for src in svg_cli:
        p = Path(src)
        if not p.exists():
            continue
        # Prefer Front/Back copper + edge
        tag = p.stem.lower()
        if "front" in tag or p.stem.endswith("F_Cu") or "F.Cu" in p.name or "Front" in p.name:
            name = "kicad_plot_F_Cu"
        elif "back" in tag or "B_Cu" in p.stem or "Back" in p.name:
            name = "kicad_plot_B_Cu"
        elif "edge" in tag.lower():
            name = "kicad_plot_Edge_Cuts"
        elif "in1" in tag.lower():
            name = "kicad_plot_In1_Cu"
        elif "in2" in tag.lower():
            name = "kicad_plot_In2_Cu"
        else:
            name = f"kicad_plot_{p.stem}"
        dest_svg = DOCS / f"{name}.svg"
        shutil.copy(p, dest_svg)
        copied.append(str(dest_svg.relative_to(ROOT)))
        dest_png = DOCS / f"{name}.png"
        if svg_to_png(dest_svg, dest_png):
            copied.append(str(dest_png.relative_to(ROOT)))

    # Also copy pcbnew plots if present
    for src in manifest.get("outputs", {}).get("svg_pcbnew") or []:
        p = Path(src)
        if p.exists():
            dest = DOCS / f"pcbnew_{p.name}"
            shutil.copy(p, dest)
            copied.append(str(dest.relative_to(ROOT)))

    index = {
        "pcb": str(pcb),
        "drc_summary": summary,
        "render_manifest": manifest,
        "docs_assets": copied,
    }
    (out / "validation_index.json").write_text(json.dumps(index, indent=2) + "\n")
    (DOCS / "README.md").write_text(
        "# KiCad official renders\n\n"
        "Generated by `scripts/generate_kicad_renders.py` using **kicad-cli** "
        "(`pcb export svg`, `pcb render`) and optional **pcbnew** PLOT_CONTROLLER.\n\n"
        f"Source board: `{pcb}`\n\n"
        f"DRC: errors={summary['error_count']}, warnings={summary['warning_count']}, "
        f"copper_issues={summary['copper_violation_count']}.\n",
        encoding="utf-8",
    )
    print("Docs assets:")
    for c in copied:
        print(" ", c)
    print("Done →", out)


if __name__ == "__main__":
    main()
