#!/usr/bin/env python3
"""Generate README figures + benchmark JSON for HALO-90 (or synthetic demo).

Usage (from repo root, venv active):
  python scripts/generate_docs_images.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Circle, Rectangle  # noqa: E402

from physics_router.config_io import example_config, load_config  # noqa: E402
from physics_router.design_rules import load_design_rules  # noqa: E402
from physics_router.kicad_io import board_from_synthetic, load_board_from_kicad_pcb  # noqa: E402
from physics_router.physics import (  # noqa: E402
    GeometricSpiceProxy,
    OpenEMSBackend,
    apply_simulation_scores,
    geometric_score,
)
from physics_router.router import clearance_aware_route, topological_guide_route  # noqa: E402
from physics_router.routing_strategies import multilayer_route, pre_route_analysis  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
CFG_PATH = ROOT / "examples/halo-90/placement_config.yaml"
OUT = ROOT / "docs" / "images"
RES = ROOT / "examples" / "halo-90"


def net_color(cfg, name: str) -> str:
    lab = cfg.net_by_name().get(name)
    if not lab:
        return "#888888"
    m = {
        "power": "#d62728",
        "ground": "#1f77b4",
        "high_speed": "#ff7f0e",
        "analog": "#2ca02c",
        "differential": "#9467bd",
        "signal": "#8c564b",
        "reset": "#e377c2",
        "clock": "#17becf",
        "rf": "#bcbd22",
    }
    key = lab.net_class.value if hasattr(lab.net_class, "value") else str(lab.net_class)
    return m.get(key, "#888888")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    RES.mkdir(parents=True, exist_ok=True)
    results: dict = {"timings_s": {}, "metrics": {}}

    if PCB.exists() and CFG_PATH.exists():
        cfg = load_config(CFG_PATH)
        board = load_board_from_kicad_pcb(PCB, cfg)
        rules = load_design_rules(PCB)
        source = "halo-90"
    else:
        cfg = example_config()
        board = board_from_synthetic(cfg)
        rules = None
        source = "synthetic"

    results["source"] = source
    results["components"] = len(board.components)
    results["nets"] = len(board.nets)
    results["copper_layers"] = list(board.copper_layers)

    t0 = time.perf_counter()
    sb = geometric_score(board, cfg)
    sb = apply_simulation_scores(
        board, cfg, sb, spice=GeometricSpiceProxy(), openems=OpenEMSBackend()
    )
    results["timings_s"]["score"] = round(time.perf_counter() - t0, 3)
    results["metrics"]["score_total"] = round(sb.total, 2)
    results["metrics"]["score_breakdown"] = {k: round(v, 2) for k, v in sb.as_dict().items()}
    results["metrics"]["physics_notes"] = sb.notes

    if rules:
        t0 = time.perf_counter()
        pr = pre_route_analysis(board, cfg, rules)
        results["timings_s"]["pre_route"] = round(time.perf_counter() - t0, 3)
        results["metrics"]["pre_route"] = pr.to_dict()

    t0 = time.perf_counter()
    guide = topological_guide_route(board, cfg)
    results["timings_s"]["route_guide"] = round(time.perf_counter() - t0, 3)
    results["metrics"]["guide"] = {
        "segments": len(guide.segments),
        "length_mm": round(guide.total_length_mm, 2),
        "vias": guide.via_count,
        "unrouted": guide.unrouted_nets,
    }

    t0 = time.perf_counter()
    if rules:
        routed = multilayer_route(
            board,
            cfg,
            rules,
            clearance_mm=max(0.15, rules.constraints.min_clearance_mm),
            grid_mm=1.0,
            allow_vias=True,
        )
    else:
        routed = clearance_aware_route(board, cfg, clearance_mm=0.2, grid_mm=1.0, allow_vias=True)
    results["timings_s"]["route_clearance_grid1mm"] = round(time.perf_counter() - t0, 3)
    results["metrics"]["route"] = {
        "segments": len(routed.segments),
        "length_mm": round(routed.total_length_mm, 2),
        "vias": routed.via_count,
        "unrouted": routed.unrouted_nets,
        "violations": routed.clearance_violations,
        "notes": routed.notes[:8],
    }

    (RES / "route_guide.json").write_text(json.dumps(guide.to_dict(), indent=2) + "\n")
    (RES / "route_result.json").write_text(json.dumps(routed.to_dict(), indent=2) + "\n")
    (RES / "benchmark_results.json").write_text(json.dumps(results, indent=2) + "\n")

    xs = [c.x_mm for c in board.components.values()]
    ys = [c.y_mm for c in board.components.values()]
    pad = 2.0
    xlim = (min(xs) - pad, max(xs) + pad)
    ylim = (min(ys) - pad, max(ys) + pad)

    # Placement
    fig, ax = plt.subplots(figsize=(8, 8), dpi=140)
    ax.set_aspect("equal")
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    if source == "halo-90":
        ax.add_patch(Circle((0, 0), 12, fill=False, linestyle="--", color="#aaaaaa", linewidth=1))
    for ref, c in board.components.items():
        color = "#4c78a8"
        if ref.startswith("D") and ref[1:].isdigit():
            color = "#f58518"
        elif ref.startswith("TP"):
            color = "#54a24b"
        elif ref in ("U1", "U2", "BT1", "MK1", "S1"):
            color = "#e45756"
        w, h = max(c.width_mm, 0.4), max(c.height_mm, 0.4)
        ax.add_patch(
            Rectangle(
                (c.x_mm - w / 2, c.y_mm - h / 2),
                w,
                h,
                facecolor=color,
                edgecolor="k",
                linewidth=0.3,
                alpha=0.85,
            )
        )
        if not (ref.startswith("D") and ref[1:].isdigit()):
            ax.text(c.x_mm, c.y_mm, ref, fontsize=5, ha="center", va="center")
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    ax.set_title(f"Placement — {source} ({len(board.components)} footprints)")
    ax.grid(True, alpha=0.3)
    ax.legend(
        handles=[
            Line2D([0], [0], marker="s", color="w", markerfacecolor="#f58518", markersize=8, label="LEDs"),
            Line2D([0], [0], marker="s", color="w", markerfacecolor="#e45756", markersize=8, label="Core"),
            Line2D([0], [0], marker="s", color="w", markerfacecolor="#54a24b", markersize=8, label="Pogo pads"),
            Line2D([0], [0], marker="s", color="w", markerfacecolor="#4c78a8", markersize=8, label="Other"),
        ],
        loc="upper right",
        fontsize=7,
    )
    fig.tight_layout()
    fig.savefig(OUT / "placement_overview.png", bbox_inches="tight")
    plt.close(fig)

    # Guide route
    fig, ax = plt.subplots(figsize=(8, 8), dpi=140)
    ax.set_aspect("equal")
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    if source == "halo-90":
        ax.add_patch(Circle((0, 0), 12, fill=False, linestyle="--", color="#cccccc", linewidth=1))
    for c in board.components.values():
        w, h = max(c.width_mm, 0.3), max(c.height_mm, 0.3)
        ax.add_patch(
            Rectangle(
                (c.x_mm - w / 2, c.y_mm - h / 2),
                w,
                h,
                facecolor="#dddddd",
                edgecolor="#999999",
                linewidth=0.2,
            )
        )
    for seg in guide.segments:
        ax.plot(
            [seg.x1, seg.x2],
            [seg.y1, seg.y2],
            color=net_color(cfg, seg.net),
            linewidth=max(0.5, seg.width_mm * 3),
            alpha=0.75,
        )
    ax.set_title(
        f"Guide route (free-angle) — {len(guide.segments)} segs, {guide.total_length_mm:.1f} mm"
    )
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT / "route_guide.png", bbox_inches="tight")
    plt.close(fig)

    # By layer
    layers = sorted({s.layer for s in routed.segments}) or ["F.Cu"]
    n = len(layers)
    fig, axes = plt.subplots(1, n, figsize=(4 * n, 4), dpi=140)
    if n == 1:
        axes = [axes]
    for ax, ly in zip(axes, layers):
        ax.set_aspect("equal")
        ax.set_xlim(*xlim)
        ax.set_ylim(*ylim)
        if source == "halo-90":
            ax.add_patch(Circle((0, 0), 12, fill=False, linestyle="--", color="#cccccc", linewidth=0.8))
        for c in board.components.values():
            w, h = max(c.width_mm, 0.3), max(c.height_mm, 0.3)
            ax.add_patch(
                Rectangle(
                    (c.x_mm - w / 2, c.y_mm - h / 2),
                    w,
                    h,
                    facecolor="#eeeeee",
                    edgecolor="#bbbbbb",
                    linewidth=0.2,
                )
            )
        segs = [s for s in routed.segments if s.layer == ly]
        for seg in segs:
            ax.plot(
                [seg.x1, seg.x2],
                [seg.y1, seg.y2],
                color=net_color(cfg, seg.net),
                linewidth=max(0.6, seg.width_mm * 4),
                alpha=0.85,
            )
        for v in routed.vias:
            ax.add_patch(
                Circle((v.x, v.y), v.size_mm / 2, facecolor="none", edgecolor="k", linewidth=0.8)
            )
        ax.set_title(f"{ly} ({len(segs)} segs)")
        ax.grid(True, alpha=0.25)
    fig.suptitle(
        f"Clearance route (grid 1 mm) — {routed.via_count} vias, {routed.total_length_mm:.1f} mm",
        fontsize=11,
    )
    fig.tight_layout()
    fig.savefig(OUT / "route_by_layer.png", bbox_inches="tight")
    plt.close(fig)

    # Score breakdown
    fig, ax = plt.subplots(figsize=(8, 4), dpi=140)
    bd = results["metrics"]["score_breakdown"]
    keys = [k for k in bd if k != "total"]
    vals = [bd[k] for k in keys]
    ax.barh(keys, vals, color=plt.cm.viridis(np.linspace(0.2, 0.9, len(keys))))
    ax.set_xlabel("Cost (lower is better)")
    ax.set_title(f"Placement score breakdown — total {bd['total']:.1f}")
    fig.tight_layout()
    fig.savefig(OUT / "score_breakdown.png", bbox_inches="tight")
    plt.close(fig)

    # Runtimes
    fig, ax = plt.subplots(figsize=(7, 3.5), dpi=140)
    items = list(results["timings_s"].items())
    ax.bar([k for k, _ in items], [v for _, v in items], color="#4c78a8")
    ax.set_ylabel("seconds")
    ax.set_title("Measured runtimes (this machine)")
    for i, (k, v) in enumerate(items):
        ax.text(i, v, f"{v:.3f}s", ha="center", va="bottom", fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "runtimes.png", bbox_inches="tight")
    plt.close(fig)

    print(json.dumps(results, indent=2))
    print(f"Images → {OUT}")
    print(f"JSON   → {RES}")


if __name__ == "__main__":
    main()
