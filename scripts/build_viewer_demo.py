#!/usr/bin/env python3
"""Build demo assets: TopoR routes, DSN, comparison, dashboard, viewer_data, optional GLB.

  python scripts/build_viewer_demo.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from physics_router.compare import compare_metrics, load_route_metrics, write_comparison_markdown
from physics_router.config_io import example_config, load_config
from physics_router.dashboard import write_dashboard
from physics_router.design_rules import load_design_rules
from physics_router.dsn_export import export_dsn, write_freerouting_readme
from physics_router.kicad_io import board_from_synthetic, load_board_from_kicad_pcb
from physics_router.kicad_tools import export_step, find_kicad_cli
from physics_router.openems_export import geometry_from_board
from physics_router.router import topological_guide_route
from physics_router.routing_strategies import multilayer_route
from physics_router.viewer_export import build_viewer_payload, write_viewer_data

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "examples" / "demo"
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
HALO_CFG = ROOT / "examples/halo-90/placement_config.yaml"


def try_glb(pcb: Path, dest: Path) -> str | None:
    cli = find_kicad_cli()
    if not cli or not pcb.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(cli),
        "pcb",
        "export",
        "glb",
        "-f",
        "-o",
        str(dest),
        "--board-only",
        "--include-tracks",
        "--include-pads",
        "--include-zones",
        "--include-inner-copper",
        "--include-soldermask",
        "--include-silkscreen",
        str(pcb),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode == 0 and dest.exists():
        return dest.name  # relative for viewer
    (dest.parent / "glb_error.txt").write_text(r.stderr or r.stdout, encoding="utf-8")
    return None


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    viewer_dir = ROOT / "viewer"
    viewer_dir.mkdir(exist_ok=True)

    # Prefer HALO-90 if present else synthetic
    if HALO_PCB.exists() and HALO_CFG.exists():
        cfg = load_config(HALO_CFG)
        board = load_board_from_kicad_pcb(HALO_PCB, cfg)
        rules = load_design_rules(HALO_PCB)
        label = "halo-90"
        pcb_path = HALO_PCB
    else:
        cfg = example_config()
        board = board_from_synthetic(cfg)
        rules = None
        label = "synthetic"
        pcb_path = None

    # Routes: guide + clearance (coarse for speed on HALO)
    guide = topological_guide_route(board, cfg)
    if rules:
        topor = multilayer_route(
            board, cfg, rules, clearance_mm=max(0.15, rules.constraints.min_clearance_mm), grid_mm=1.0
        )
    else:
        from physics_router.router import clearance_aware_route

        topor = clearance_aware_route(board, cfg, clearance_mm=0.2, grid_mm=0.5)

    (OUT / "topor_route.json").write_text(json.dumps(topor.to_dict(), indent=2) + "\n")
    (OUT / "guide_route.json").write_text(json.dumps(guide.to_dict(), indent=2) + "\n")

    # DSN for FreeRouting
    dsn_path = export_dsn(board, OUT / "board.dsn", config=cfg, rules=rules, board_name=label)
    write_freerouting_readme(OUT)

    # Comparison (TopoR only unless SES present)
    topor_m = load_route_metrics(OUT / "topor_route.json")
    ses = OUT / "board.ses"
    baseline = None
    if ses.exists():
        from physics_router.compare import parse_ses_metrics

        baseline = parse_ses_metrics(ses)
    comparison = compare_metrics(topor_m, baseline)
    (OUT / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
    write_comparison_markdown(comparison, OUT / "comparison.md")
    # Also copy markdown snippet path for README
    shutil.copy(OUT / "comparison.md", ROOT / "docs" / "route_comparison.md")

    # EMI geometry
    prims = geometry_from_board(board, routes=topor, config=cfg)
    emi = {
        "primitives": [p.to_dict() for p in prims[:500]],
        "note": "subset of copper primitives for EMI visualization",
    }
    (OUT / "emi_geometry.json").write_text(json.dumps(emi, indent=2) + "\n")

    # Optional GLB / STEP
    glb_name = None
    step_name = None
    if pcb_path:
        glb_name = try_glb(pcb_path, OUT / "board.glb")
        try:
            export_step(
                pcb_path,
                OUT / "board_sim.step",
                board_only=True,
                no_components=True,
                include_tracks=True,
                include_pads=True,
                include_soldermask=True,
                include_silkscreen=True,
                include_inner_copper=True,
            )
            step_name = "board_sim.step"
        except Exception as e:
            (OUT / "step_error.txt").write_text(str(e), encoding="utf-8")

    payload = build_viewer_payload(
        board,
        cfg,
        routes={"topor": topor, "guide": guide},
        include_score=True,
        glb_url=glb_name,
        step_url=step_name,
        emi_geometry_url="emi_geometry.json",
        comparison=comparison,
        extra={"label": label, "dsn": dsn_path.name},
    )
    payload["emi_geometry"] = emi
    write_viewer_data(payload, OUT / "viewer_data.json")
    # copy next to viewer for relative fetch
    shutil.copy(OUT / "viewer_data.json", viewer_dir / "viewer_data.json")
    if glb_name and (OUT / glb_name).exists():
        shutil.copy(OUT / glb_name, viewer_dir / glb_name)

    # Dashboard
    write_dashboard(
        OUT / "dashboard.html",
        payload.get("physics") or {},
        title=f"Physics budget — {label}",
        board_meta={
            "label": label,
            "components": len(board.components),
            "nets": len(board.nets),
            "layers": ", ".join(board.copper_layers),
        },
        routes=payload.get("routes"),
        comparison=comparison,
        viewer_url="../../viewer/index.html",
    )
    # viewer-relative dashboard link convenience
    write_dashboard(
        viewer_dir / "dashboard.html",
        payload.get("physics") or {},
        title=f"Physics budget — {label}",
        board_meta={
            "label": label,
            "components": len(board.components),
            "nets": len(board.nets),
            "layers": ", ".join(board.copper_layers),
        },
        routes=payload.get("routes"),
        comparison=comparison,
        viewer_url="index.html",
    )

    # Baselines file for CI
    baseline_path = ROOT / "ci" / "baselines" / "scores.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    scores = {
        "synthetic_or_halo": label,
        "score_total": (payload.get("physics") or {}).get("score", {}).get("total"),
        "topor_length_mm": topor.total_length_mm,
        "topor_vias": topor.via_count,
        "guide_length_mm": guide.total_length_mm,
    }
    # merge with existing baselines preserving synthetic fixed key
    if baseline_path.exists():
        old = json.loads(baseline_path.read_text(encoding="utf-8"))
    else:
        old = {}
    old[label] = scores
    # Always refresh synthetic baseline quickly
    if label != "synthetic":
        scfg = example_config()
        sboard = board_from_synthetic(scfg)
        from physics_router.physics import geometric_score

        old["synthetic"] = {
            "score_total": geometric_score(sboard, scfg).total,
            "note": "placement geometric total only",
        }
    else:
        old["synthetic"] = scores
    baseline_path.write_text(json.dumps(old, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"out": str(OUT), "label": label, "glb": glb_name, "step": step_name}, indent=2))


if __name__ == "__main__":
    main()
