#!/usr/bin/env python3
"""Benchmark mppcInterface v1.3 (commit 580c61d) human vs topological autorouter.

  python scripts/run_mppc_benchmark.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PCB = ROOT / "examples/mppc-interface/mppcInterface_v1.3.kicad_pcb"
CFG = ROOT / "examples/mppc-interface/placement_config.yaml"
OUT = ROOT / "viewer/runs/mppc_v1.3"
IMG = ROOT / "docs/images/golden"
REPORT = ROOT / "docs/MPPC_BENCHMARK.md"


def main() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from physics_router.config_io import load_config
    from physics_router.golden_eval import evaluate_board
    from physics_router.graph_theory import plan_graph_topology
    from physics_router.kicad_io import load_board_from_kicad_pcb
    from physics_router.router import extract_routes_from_kicad_pcb

    OUT.mkdir(parents=True, exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)

    config = load_config(CFG)
    board = load_board_from_kicad_pcb(PCB, config)
    human = extract_routes_from_kicad_pcb(PCB, board_nets=board.nets)
    (OUT / "human_route.json").write_text(
        json.dumps(human.to_dict(), indent=2) + "\n", encoding="utf-8"
    )

    topo = plan_graph_topology(board, config, use_overflow_steiner=True)
    (OUT / "topology.json").write_text(
        json.dumps(topo.to_dict(), indent=2) + "\n", encoding="utf-8"
    )

    # Long multipin board: always inline (timeout_s=0) and no hard process kill.
    # hard_deadline=True + positive timeout was killing capacity search mid-net.
    entry = {
        "id": "mppc_v1.3",
        "pcb": str(PCB),
        "config": str(CFG),
        "expect": "partial_ok",
        "timeout_s": 0,  # inline route (no spawn)
        "min_completion": 0.0,
        "difficulty": "hard",
        "cbs_repair": False,  # full CBS after 85 nets is very expensive
        "hard_deadline": False,
        "_base": str(ROOT),
    }
    print(
        f"mppc v1.3: {board.width_mm:.1f}×{board.height_mm:.1f}mm · "
        f"{len(board.components)} parts · {len(board.nets)} nets · "
        f"{board.copper_layers}"
    )
    print(
        f"human: segs={len(human.segments)} vias={human.via_count} "
        f"areas={len(human.areas)} L={human.total_length_mm:.1f}mm "
        f"unrouted={len(human.unrouted_nets)}"
    )
    print(
        f"topo: guide_L={topo.metrics.get('guide_length_mm')} "
        f"steiner={topo.metrics.get('steiner_net_count')} "
        f"cut_ok={ (topo.metrics.get('cut_preflight') or {}).get('feasible_under_model')}"
    )
    progress_path = OUT / "progress.json"
    progress_path.write_text(
        json.dumps(
            {
                "status": "routing",
                "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "pipeline": "capacity",
                "effort": 0.55,
                "hard_deadline": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("routing (capacity pipeline, inline, no hard deadline, effort 0.55)…")
    t0 = time.time()
    row = evaluate_board(
        entry,
        pipeline="capacity",
        effort=0.55,
        out_dir=OUT,
        hard_deadline=False,
        cbs_repair=False,
    )
    elapsed = time.time() - t0
    row["benchmark_wall_s"] = round(elapsed, 2)
    row["source_commit"] = "580c61d"
    row["source_repo"] = "https://github.com/muonTelescope/mppcInterface"
    (OUT / "benchmark_row.json").write_text(
        json.dumps(row, indent=2, default=str) + "\n", encoding="utf-8"
    )
    progress_path.write_text(
        json.dumps(
            {
                "status": "done" if not row.get("error") else "error",
                "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "elapsed_s": round(elapsed, 2),
                "grade": row.get("golden_grade"),
                "score": row.get("golden_score"),
                "completion": row.get("completion_ratio"),
                "hard_drc": row.get("hard_violations"),
                "error": row.get("error"),
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"done {elapsed:.1f}s grade={row.get('golden_grade')} "
        f"score={row.get('golden_score')} completion={row.get('completion_ratio')} "
        f"hard_drc={row.get('hard_violations')} error={row.get('error')}"
    )

    # --- images ---
    def load_json(p: Path) -> dict:
        if not p.is_file():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))

    h = load_json(OUT / "human_route.json")
    a = load_json(OUT / "ar_route.json")

    def draw_copper(ax, data, title, color):
        segs = data.get("segments") or []
        vias = data.get("vias") or []
        for s in segs:
            ax.plot(
                [s["x1"], s["x2"]],
                [s["y1"], s["y2"]],
                color=color,
                lw=max(0.35, float(s.get("width_mm") or 0.15) * 2.2),
                alpha=0.9,
                solid_capstyle="round",
            )
        if vias:
            ax.scatter(
                [v["x"] for v in vias],
                [v["y"] for v in vias],
                s=10,
                c="#ef4444",
                zorder=5,
            )
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_title(
            f"{title}\nsegs={len(segs)} vias={len(vias)} "
            f"L={float(data.get('total_length_mm') or 0):.0f}mm"
        )
        ax.grid(True, alpha=0.25)
        ax.invert_yaxis()
        ax.set_xlabel("mm")
        ax.set_ylabel("mm")

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    draw_copper(axes[0], h, "Human route (v1.3 golden)", "#64748b")
    draw_copper(axes[1], a if a else h, "Autorouter (capacity pipeline)", "#2563eb")
    if not a:
        axes[1].text(
            0.5,
            0.5,
            "no AR copper\n(open / timeout / error)",
            ha="center",
            va="center",
            transform=axes[1].transAxes,
            fontsize=11,
            color="#94a3b8",
        )
    fig.suptitle(
        "mppcInterface v1.3 — human vs topological autorouter",
        fontweight="bold",
    )
    fig.tight_layout()
    fig.savefig(IMG / "mppc_v13_compare.png", dpi=140)
    plt.close(fig)

    # metric bars
    ar = row.get("ar") or {}
    hu = row.get("human") or {}
    fig, ax = plt.subplots(figsize=(8, 4.2))
    labels = ["Length mm", "Vias", "Segments", "Completion %"]
    human_v = [
        float(hu.get("length_mm") or h.get("total_length_mm") or 0),
        float(hu.get("vias") or h.get("via_count") or 0),
        float(hu.get("segments") or len(h.get("segments") or [])),
        100.0,
    ]
    ar_v = [
        float(ar.get("length_mm") or a.get("total_length_mm") or 0),
        float(ar.get("vias") or a.get("via_count") or 0),
        float(ar.get("segments") or len(a.get("segments") or [])),
        100.0 * float(row.get("completion_ratio") or 0),
    ]
    import numpy as np

    x = np.arange(len(labels))
    ax.bar(x - 0.2, human_v, 0.4, label="Human", color="#64748b")
    ax.bar(x + 0.2, ar_v, 0.4, label="Autorouter", color="#2563eb")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_title(
        f"mppc v1.3 metrics · grade={row.get('golden_grade')} "
        f"score={row.get('golden_score')} hard_drc={row.get('hard_violations')}"
    )
    ax.legend()
    if max(human_v[:3] + ar_v[:3]) > 0:
        ax.set_yscale("symlog", linthresh=1)
    fig.tight_layout()
    fig.savefig(IMG / "mppc_v13_metrics.png", dpi=140)
    plt.close(fig)

    # layer length human
    by_layer: dict[str, float] = {}
    for s in h.get("segments") or []:
        by_layer[s.get("layer", "?")] = by_layer.get(s.get("layer", "?"), 0.0) + (
            (s["x2"] - s["x1"]) ** 2 + (s["y2"] - s["y1"]) ** 2
        ) ** 0.5
    if by_layer:
        fig, ax = plt.subplots(figsize=(7, 3.5))
        layers = sorted(by_layer)
        ax.barh(layers, [by_layer[L] for L in layers], color="#0ea5e9")
        ax.set_xlabel("Human track length (mm)")
        ax.set_title("mppc v1.3 human copper by layer")
        fig.tight_layout()
        fig.savefig(IMG / "mppc_v13_human_layers.png", dpi=140)
        plt.close(fig)

    # write report
    missing = row.get("missing_nets") or []
    status = (
        "TIMEOUT"
        if row.get("timed_out")
        else ("ERROR" if row.get("error") and not ar else ("PASS" if row.get("passed") else "FAIL"))
    )
    lines = [
        "# Benchmark: mppcInterface v1.3 (human vs topological autorouter)",
        "",
        "**Primary golden board for physicsRouter.** HEP SiPM/MPPC readout from",
        "[muonTelescope/mppcInterface](https://github.com/muonTelescope/mppcInterface)",
        f"commit **`580c61d`** (*Initial update to 1.3*, 2020-08-21).",
        "",
        "Design lineage includes sPHENIX-class bias/coincidence ideas (see upstream readme).",
        "This revision is the best **electrically complete** human route in the repo history",
        "(0 nets without copper; 4-layer stack; pours present).",
        "",
        "---",
        "",
        "## Board facts",
        "",
        "| Item | Value |",
        "|------|-------|",
        f"| Outline | **{board.width_mm:.1f} × {board.height_mm:.1f} mm** |",
        f"| Components | **{len(board.components)}** |",
        f"| Nets | **{len(board.nets)}** |",
        f"| Copper layers | `{', '.join(board.copper_layers)}` |",
        f"| Human segments | **{len(human.segments)}** |",
        f"| Human vias | **{human.via_count}** |",
        f"| Human areas (pours) | **{len(human.areas)}** |",
        f"| Human length | **{human.total_length_mm:.1f} mm** |",
        f"| Human unrouted | **{len(human.unrouted_nets)}** |",
        f"| Topology guide length | {topo.metrics.get('guide_length_mm')} mm |",
        f"| Steiner multipin nets | {topo.metrics.get('steiner_net_count')} |",
        f"| Cut preflight feasible | {(topo.metrics.get('cut_preflight') or {}).get('feasible_under_model')} |",
        "",
        "Pinned files: `examples/mppc-interface/mppcInterface_v1.3.kicad_pcb` (+ `.kicad_pro`).",
        "",
        "---",
        "",
        "## Human vs autorouter",
        "",
        "![compare](images/golden/mppc_v13_compare.png)",
        "",
        "![metrics](images/golden/mppc_v13_metrics.png)",
        "",
        "![human layers](images/golden/mppc_v13_human_layers.png)",
        "",
        "## Score vs human copper",
        "",
        "| Metric | Human | Autorouter |",
        "|--------|------:|-----------:|",
        f"| Status | golden | **{status}** |",
        f"| Golden grade | — | **{row.get('golden_grade')}** |",
        f"| Golden score | — | **{row.get('golden_score')}** |",
        f"| Completion vs human nets | 100% | **{row.get('completion_ratio')}** |",
        f"| Hard DRC | 0 (assumed fabbed) | **{row.get('hard_violations')}** |",
        f"| Length (mm) | {human.total_length_mm:.1f} | {(ar or {}).get('length_mm', '—')} |",
        f"| Vias | {human.via_count} | {(ar or {}).get('vias', '—')} |",
        f"| Segments | {len(human.segments)} | {(ar or {}).get('segments', '—')} |",
        f"| Areas/pours | {len(human.areas)} | {(ar or {}).get('areas', '—')} |",
        f"| Wall time (s) | — | {row.get('time_s') or elapsed:.1f} |",
        f"| Pipeline | hand | capacity · effort 0.45 · no hard deadline · CBS off |",
        "",
        f"Missing nets vs human ({len(missing)}): "
        + (f"`{', '.join(missing[:24])}`" if missing else "_none_"),
        "",
        "### Policy reading",
        "",
        "- **Completion < 1 with hard_drc = 0** is an *honest partial*: open copper beat shorts.",
        "- Length shorter than human is only “better” if completion ≈ 1.0.",
        "- Human 4-layer pours (61 areas) are a return-path asset the AR still under-uses.",
        "",
        "---",
        "",
        "## Why this board for topological autorouting",
        "",
        "1. **Real HEP instrument** (SiPM bias, analog front-end, FPGA coincidence, Pi host).",
        "2. **Complete human multilayer golden** at `580c61d` (HEAD is a later 2L/lib revision with open nets).",
        "3. Stresses **power + HV + analog + digital** together — not a toy cross-over.",
        "4. Fits the project scope: **topology (Steiner/capacity) → free-angle geometry → 0 hard DRC**.",
        "",
        "### History note",
        "",
        "Earlier commits (`8aa2399`→`a98f88b`) show progressive 2-layer routing; v1.3 is the",
        "clean multilayer snapshot. See git log on `muonTelescope/mppcInterface`.",
        "",
        "---",
        "",
        "## Reproduce",
        "",
        "```bash",
        "bash scripts/build_native.sh",
        "# PCB already pinned under examples/mppc-interface/",
        "python scripts/run_mppc_benchmark.py",
        "",
        "physics-router golden-eval \\",
        "  --id mppc_v1.3 \\",
        "  --manifest examples/mppc-interface/manifest.yaml \\",
        "  --pipeline capacity --effort 0.5",
        "```",
        "",
        "Artifacts: `viewer/runs/mppc_v1.3/` · images: `docs/images/golden/mppc_v13_*.png`.",
        "",
        f"_Generated {time.strftime('%Y-%m-%d')} · physicsRouter topological autorouter._",
        "",
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {REPORT}")
    print(f"Images: {IMG}/mppc_v13_*.png")


if __name__ == "__main__":
    main()
