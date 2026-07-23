#!/usr/bin/env python3
"""Inventory golden corpus, optional rip-and-reroute, charts + physics report.

  python scripts/golden_corpus_analyze.py
  python scripts/golden_corpus_analyze.py --route-easy
  python scripts/golden_corpus_analyze.py --route-ids simple_2net,pq9_devboard
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "viewer" / "runs" / "golden_corpus"
IMG = ROOT / "docs" / "images" / "golden"
REPORT = ROOT / "docs" / "GOLDEN_CORPUS.md"

# Human taxonomy used for physics narrative + chart coloring
DOMAIN: dict[str, str] = {
    "simple_2net": "fixture",
    "halo-90": "consumer_dense",
    "ofm_illumination": "science_easy",
    "openflexure_illum": "science_easy",
    "pq9_devboard": "space_cubsat",
    "ecc83_pp": "audio_analog",
    "ecc83_pp_v2": "audio_analog",
    "sonde_xilinx": "daq_fpga",
    "complex_hierarchy": "edu",
    "stickhub": "usb_hub",
    "multichannel_mixer": "audio_mixed",
    "interf_u": "instrumentation",
    "royalblue_feather": "wireless_mcu",
    "pic_programmer": "tooling",
    "openipmc_hw": "hep_atca",
    "satnogs_comms": "space_comms",
    "tinytapeout": "asic_carrier",
    "one_air_max": "consumer",
    "cm5_minima": "sbc_carrier",
    "kit_coldfire_xilinx": "fpga_dev",
    "video": "video_mixed",
    "jetson_nano": "sbc_carrier",
    "jetson_agx_thor": "sbc_carrier",
    "vme_wren": "hep_timing",
}

DIFFICULTY: dict[str, str] = {
    "simple_2net": "easy",
    "halo-90": "hard",
    "ofm_illumination": "easy",
    "openflexure_illum": "easy",
    "pq9_devboard": "easy",
    "ecc83_pp": "easy",
    "ecc83_pp_v2": "easy",
    "sonde_xilinx": "easy",
    "complex_hierarchy": "easy",
    "stickhub": "medium",
    "multichannel_mixer": "medium",
    "interf_u": "medium",
    "royalblue_feather": "medium",
    "pic_programmer": "medium",
    "openipmc_hw": "hard",
    "satnogs_comms": "hard",
    "tinytapeout": "hard",
    "one_air_max": "hard",
    "cm5_minima": "hard",
    "kit_coldfire_xilinx": "hard",
    "video": "hard",
    "jetson_nano": "extreme",
    "jetson_agx_thor": "extreme",
    "vme_wren": "extreme",
}

# Map id → pcb path relative to repo root
BOARD_PATHS: dict[str, str] = {
    "simple_2net": "tests/fixtures/golden/simple_2net.kicad_pcb",
    "halo-90": "third_party/halo-90/pcb/halo-90.kicad_pcb",
    "ofm_illumination": "third_party/golden/ofm-led/ofm_cc_illumination.kicad_pcb",
    "openflexure_illum": "third_party/golden/openflexure-illum/SimpleIllumination.kicad_pcb",
    "pq9_devboard": "third_party/golden/pq9-devboard/pq9-devboard.kicad_pcb",
    "ecc83_pp": "third_party/golden/kicad-demos/ecc83/ecc83-pp.kicad_pcb",
    "ecc83_pp_v2": "third_party/golden/kicad-demos/ecc83/ecc83-pp_v2.kicad_pcb",
    "sonde_xilinx": "third_party/golden/kicad-demos/sonde xilinx/sonde xilinx.kicad_pcb",
    "complex_hierarchy": "third_party/golden/kicad-demos/complex_hierarchy/complex_hierarchy.kicad_pcb",
    "stickhub": "third_party/golden/kicad-demos/stickhub/StickHub.kicad_pcb",
    "multichannel_mixer": "third_party/golden/kicad-demos/multichannel/multichannel_mixer.kicad_pcb",
    "interf_u": "third_party/golden/kicad-demos/interf_u/interf_u.kicad_pcb",
    "royalblue_feather": "third_party/golden/kicad-demos/royalblue54L_feather/RoyalBlue54L-Feather.kicad_pcb",
    "pic_programmer": "third_party/golden/kicad-demos/pic_programmer/pic_programmer.kicad_pcb",
    "openipmc_hw": "third_party/golden/openipmc-hw/openipmc-hw.kicad_pcb",
    "satnogs_comms": "third_party/golden/satnogs-comms/satnogs-comms.kicad_pcb",
    "tinytapeout": "third_party/golden/kicad-demos/tiny_tapeout/tinytapeout-demo.kicad_pcb",
    "one_air_max": "third_party/golden/kicad-demos/openair-max/One-Air-Max.kicad_pcb",
    "cm5_minima": "third_party/golden/kicad-demos/cm5_minima/CM5_MINIMA_3.kicad_pcb",
    "kit_coldfire_xilinx": "third_party/golden/kicad-demos/kit-dev-coldfire-xilinx_5213/kit-dev-coldfire-xilinx_5213.kicad_pcb",
    "video": "third_party/golden/kicad-demos/video/video.kicad_pcb",
    "jetson_nano": "third_party/golden/antmicro-jetson-nano/jetson-nano-baseboard.kicad_pcb",
    "jetson_agx_thor": "third_party/golden/kicad-demos/jetson-agx-thor-baseboard/jetson-agx-thor-baseboard.kicad_pcb",
    "vme_wren": "third_party/golden/kicad-demos/vme-wren/vme-wren.kicad_pcb",
}


def _layer_stats(segments) -> dict[str, Any]:
    by: dict[str, float] = {}
    for s in segments:
        d = math.hypot(s.x2 - s.x1, s.y2 - s.y1)
        by[s.layer] = by.get(s.layer, 0.0) + d
    total = sum(by.values()) or 1.0
    copper = [ly for ly in by if ly.endswith(".Cu") or "Cu" in ly]
    return {
        "length_by_layer_mm": {k: round(v, 2) for k, v in sorted(by.items(), key=lambda x: -x[1])},
        "n_copper_layers_used": len(copper),
        "outer_fraction": round(
            (by.get("F.Cu", 0) + by.get("B.Cu", 0)) / total, 3
        ),
        "inner_fraction": round(
            sum(v for k, v in by.items() if k not in ("F.Cu", "B.Cu") and "Cu" in k)
            / total,
            3,
        ),
    }


def _via_density(vias: int, length_mm: float) -> float:
    if length_mm <= 0:
        return 0.0
    return round(1000.0 * vias / length_mm, 3)  # vias per meter of track


def inventory() -> list[dict[str, Any]]:
    from physics_router.router import extract_routes_from_kicad_pcb

    rows: list[dict[str, Any]] = []
    for bid, rel in BOARD_PATHS.items():
        pcb = ROOT / rel
        if not pcb.is_file():
            rows.append({"id": bid, "missing": True, "path": rel})
            continue
        t0 = time.time()
        human = extract_routes_from_kicad_pcb(pcb)
        ly = _layer_stats(human.segments)
        nets = {s.net for s in human.segments if s.net} | {
            v.net for v in human.vias if v.net
        }
        # crude bbox from copper
        xs = [s.x1 for s in human.segments] + [s.x2 for s in human.segments]
        ys = [s.y1 for s in human.segments] + [s.y2 for s in human.segments]
        if human.vias:
            xs += [v.x for v in human.vias]
            ys += [v.y for v in human.vias]
        if xs:
            w = max(xs) - min(xs)
            h = max(ys) - min(ys)
            area = max(w * h, 1.0)
        else:
            w = h = area = 0.0
        row = {
            "id": bid,
            "path": rel,
            "missing": False,
            "domain": DOMAIN.get(bid, "other"),
            "difficulty": DIFFICULTY.get(bid, "unknown"),
            "segments": len(human.segments),
            "vias": human.via_count,
            "length_mm": round(human.total_length_mm, 2),
            "nets_with_copper": len(nets),
            "extract_s": round(time.time() - t0, 3),
            "bbox_mm": [round(w, 2), round(h, 2)],
            "track_density_mm_per_cm2": round(human.total_length_mm / (area / 100.0), 3)
            if area
            else 0.0,
            "via_per_m": _via_density(human.via_count, human.total_length_mm),
            **ly,
            "physics_class": _physics_class(bid, human.via_count, ly),
        }
        rows.append(row)
        print(
            f"  {bid:22s} segs={row['segments']:6d} vias={row['vias']:5d} "
            f"L={row['length_mm']:9.1f} nets={row['nets_with_copper']:4d} "
            f"diff={row['difficulty']:8s} {row['physics_class']}"
        )
    return rows


def _physics_class(bid: str, vias: int, ly: dict[str, Any]) -> str:
    """Label human routing style for physics narrative."""
    outer = float(ly.get("outer_fraction") or 0)
    layers = int(ly.get("n_copper_layers_used") or 0)
    if bid in ("ecc83_pp", "ecc83_pp_v2"):
        return "single_layer_analog"
    if vias == 0 and layers <= 2:
        return "planar_or_pour_heavy"
    if vias == 0 and layers >= 4:
        return "multilayer_no_via_extract"  # zones/microvias/other
    if outer > 0.85 and vias < 50:
        return "outer_prefer_signal"
    if layers >= 6 and vias > 100:
        return "hs_multilayer_via_farm"
    if layers >= 4 and float(ly.get("inner_fraction") or 0) > 0.35:
        return "inner_signal_stripline"
    if vias > 200:
        return "via_dense_escape"
    return "general_digital"


def route_board(bid: str, *, effort: float = 0.45, timeout_s: float = 90.0) -> dict[str, Any]:
    from physics_router.compare import compare_to_golden
    from physics_router.config_io import example_config
    from physics_router.design_rules import default_design_rules, load_design_rules
    from physics_router.kicad_io import load_board_from_kicad_pcb
    from physics_router.net_import import import_labels_to_config
    from physics_router.router import (
        append_routes_to_kicad_pcb,
        extract_routes_from_kicad_pcb,
    )

    rel = BOARD_PATHS[bid]
    pcb = ROOT / rel
    cfg = import_labels_to_config(example_config(), pcb_path=pcb)
    cfg.project_name = bid
    board = load_board_from_kicad_pcb(pcb, cfg)
    rules = load_design_rules(pcb_path=pcb) or default_design_rules()
    human = extract_routes_from_kicad_pcb(pcb, board_nets=board.nets)

    t0 = time.time()
    try:
        from physics_router.route_pipeline import run_capacity_pipeline

        ar = run_capacity_pipeline(
            board, cfg, rules, effort=float(effort), raise_on_fail=False
        )
        err = None
    except Exception as exc:
        ar = None
        err = str(exc)
    elapsed = time.time() - t0

    out_dir = OUT / "routes" / bid
    out_dir.mkdir(parents=True, exist_ok=True)
    if ar is None:
        return {
            "id": bid,
            "error": err,
            "time_s": round(elapsed, 2),
            "passed": False,
        }

    (out_dir / "ar_route.json").write_text(
        json.dumps(ar.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    append_routes_to_kicad_pcb(
        str(pcb),
        str(out_dir / f"{bid}_ar.kicad_pcb"),
        ar,
        clear_existing_copper=True,
    )
    hard = int(ar.clearance_violations or 0)
    cmp = compare_to_golden(ar, human, hard_violations=hard)
    (out_dir / "golden_compare.json").write_text(
        json.dumps(cmp, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "id": bid,
        "time_s": round(elapsed, 2),
        "timeout_warning": elapsed > timeout_s,
        "ar_segments": len(ar.segments),
        "ar_vias": ar.via_count,
        "ar_length_mm": round(ar.total_length_mm, 2),
        "ar_unrouted": len(ar.unrouted_nets),
        "human_segments": len(human.segments),
        "human_vias": human.via_count,
        "human_length_mm": round(human.total_length_mm, 2),
        "completion_ratio": (cmp.get("completion") or {}).get("ratio"),
        "golden_score": cmp.get("golden_score"),
        "golden_grade": cmp.get("golden_grade"),
        "hard_violations": hard,
        "missing_nets": (cmp.get("completion") or {}).get("missing_nets"),
        "passed": hard == 0,
    }


def make_charts(inv: list[dict[str, Any]], routes: list[dict[str, Any]]) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    IMG.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    present = [r for r in inv if not r.get("missing") and r.get("segments", 0) > 0]
    present.sort(key=lambda r: r["length_mm"])

    # 1) Human copper scale
    fig, ax = plt.subplots(figsize=(12, 6))
    ids = [r["id"] for r in present]
    x = np.arange(len(ids))
    ax.bar(x, [r["length_mm"] for r in present], color="#3b82f6", alpha=0.85, label="length mm")
    ax2 = ax.twinx()
    ax2.plot(x, [r["vias"] for r in present], "o-", color="#ef4444", label="vias")
    ax.set_xticks(x)
    ax.set_xticklabels(ids, rotation=55, ha="right", fontsize=8)
    ax.set_ylabel("Human track length (mm)")
    ax2.set_ylabel("Via count")
    ax.set_title("Golden corpus — human copper scale")
    ax.set_yscale("log")
    ax2.set_yscale("symlog")
    fig.tight_layout()
    p = IMG / "01_human_scale.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p.relative_to(ROOT)))

    # 2) Layer use / outer vs inner
    fig, ax = plt.subplots(figsize=(12, 5))
    outer = [r.get("outer_fraction", 0) for r in present]
    inner = [r.get("inner_fraction", 0) for r in present]
    ax.bar(x, outer, label="outer (F+B) fraction", color="#22c55e")
    ax.bar(x, inner, bottom=outer, label="inner Cu fraction", color="#a855f7")
    ax.set_xticks(x)
    ax.set_xticklabels(ids, rotation=55, ha="right", fontsize=8)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("Fraction of track length")
    ax.set_title("Layer strategy — outer vs inner copper (human)")
    ax.legend(loc="upper right")
    fig.tight_layout()
    p = IMG / "02_layer_strategy.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p.relative_to(ROOT)))

    # 3) Via density vs difficulty
    fig, ax = plt.subplots(figsize=(8, 6))
    colors = {
        "easy": "#22c55e",
        "medium": "#eab308",
        "hard": "#f97316",
        "extreme": "#ef4444",
        "unknown": "#94a3b8",
    }
    for r in present:
        ax.scatter(
            r["length_mm"],
            max(r["via_per_m"], 0.01),
            s=40 + 4 * r.get("nets_with_copper", 1),
            c=colors.get(r["difficulty"], "#94a3b8"),
            alpha=0.85,
            edgecolors="k",
            linewidths=0.3,
        )
        ax.annotate(r["id"], (r["length_mm"], max(r["via_per_m"], 0.01)), fontsize=6, alpha=0.8)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Human length (mm)")
    ax.set_ylabel("Vias per meter of track")
    ax.set_title("Via density vs scale (marker size ∝ net count)")
    for d, c in colors.items():
        ax.scatter([], [], c=c, label=d)
    ax.legend(title="difficulty")
    fig.tight_layout()
    p = IMG / "03_via_density.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p.relative_to(ROOT)))

    # 4) Domain groups
    fig, ax = plt.subplots(figsize=(10, 5))
    domains: dict[str, list[float]] = {}
    for r in present:
        domains.setdefault(r["domain"], []).append(r["length_mm"])
    dnames = sorted(domains.keys(), key=lambda d: -sum(domains[d]))
    ax.barh(dnames, [sum(domains[d]) for d in dnames], color="#0ea5e9")
    ax.set_xlabel("Total human track length in corpus (mm)")
    ax.set_title("Corpus mass by application domain")
    fig.tight_layout()
    p = IMG / "04_domain_mass.png"
    fig.savefig(p, dpi=140)
    plt.close(fig)
    saved.append(str(p.relative_to(ROOT)))

    # 5) AR vs human if routes exist
    ok_routes = [r for r in routes if r.get("completion_ratio") is not None]
    if ok_routes:
        fig, ax = plt.subplots(figsize=(10, 5))
        rid = [r["id"] for r in ok_routes]
        rx = np.arange(len(rid))
        ax.bar(rx - 0.2, [r["human_length_mm"] for r in ok_routes], 0.4, label="human L", color="#64748b")
        ax.bar(rx + 0.2, [r["ar_length_mm"] for r in ok_routes], 0.4, label="AR L", color="#3b82f6")
        ax.set_xticks(rx)
        ax.set_xticklabels(rid, rotation=40, ha="right")
        ax.set_ylabel("Length (mm)")
        ax.set_title("Autorouter vs human length (routed subset)")
        ax.legend()
        fig.tight_layout()
        p = IMG / "05_ar_vs_human_length.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        saved.append(str(p.relative_to(ROOT)))

        fig, ax = plt.subplots(figsize=(10, 4))
        ax.bar(rx, [100 * float(r["completion_ratio"] or 0) for r in ok_routes], color="#22c55e")
        ax.set_xticks(rx)
        ax.set_xticklabels(rid, rotation=40, ha="right")
        ax.set_ylabel("Completion vs human nets (%)")
        ax.set_ylim(0, 105)
        ax.set_title("Rip-and-reroute completion (atomic full-net policy)")
        for i, r in enumerate(ok_routes):
            ax.text(i, 100 * float(r["completion_ratio"] or 0) + 2, r.get("golden_grade", ""), ha="center", fontsize=8)
        fig.tight_layout()
        p = IMG / "06_ar_completion.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        saved.append(str(p.relative_to(ROOT)))

        fig, ax = plt.subplots(figsize=(8, 5))
        for r in ok_routes:
            ax.scatter(
                r["time_s"],
                100 * float(r["completion_ratio"] or 0),
                s=80,
                c=colors.get(DIFFICULTY.get(r["id"], "unknown"), "#94a3b8"),
            )
            ax.annotate(r["id"], (r["time_s"], 100 * float(r["completion_ratio"] or 0)), fontsize=7)
        ax.set_xlabel("Route time (s)")
        ax.set_ylabel("Completion %")
        ax.set_title("Time vs completion (physical search cost)")
        fig.tight_layout()
        p = IMG / "07_time_vs_completion.png"
        fig.savefig(p, dpi=140)
        plt.close(fig)
        saved.append(str(p.relative_to(ROOT)))

    return saved


def write_report(inv: list[dict[str, Any]], routes: list[dict[str, Any]], charts: list[str]) -> None:
    present = [r for r in inv if not r.get("missing")]
    lines = [
        "# Golden corpus: HEP, CERN-class, and open hardware boards",
        "",
        "**TL;DR:** Human-routed open boards form a rip-and-reroute suite. Charts show copper scale, layer strategy, via density, and autorouter completion — with **physical reasons** behind decisions.",
        "",
        "```bash",
        "python scripts/golden_corpus_analyze.py           # inventory + charts",
        "python scripts/golden_corpus_analyze.py --route-easy",
        "physics-router golden-eval --manifest examples/golden/manifest.yaml --extract-only",
        "```",
        "",
        "Artifacts: `viewer/runs/golden_corpus/` · images: `docs/images/golden/`",
        "",
        "---",
        "",
        "## Why these boards (physics + experiment context)",
        "",
        "| Domain | Boards | What human routing optimizes for |",
        "|--------|--------|----------------------------------|",
        "| **HEP timing (CERN White Rabbit)** | `vme_wren` | Sub-ns sync: controlled impedance, short clock trees, layer-referenced striplines, minimal stubs on multi-Gb links |",
        "| **HEP ATCA mgmt** | `openipmc_hw` | Hot-swap / IPMI: power planes first, service nets, isolation of management from payload noise |",
        "| **Space / cubesat** | `pq9_devboard`, `satnogs_comms` | Mass/power, RF keepouts, CAN/differential pairs, thermal vias under regulators |",
        "| **SBC / FPGA carriers** | Jetson, CM5, Coldfire+Xilinx, TinyTapeout | BGA escape: via farms, HDI-like density, length-matched memory buses |",
        "| **Instrumentation / audio** | `interf_u`, `ecc83_*`, mixer | Analog: single-layer or guarded returns, star grounds, keep digital edges off sensitive nets |",
        "| **Dense matrix (in-repo)** | `halo-90` | Charlieplex: annular topology, radial escapes, open > short |",
        "",
        "PHENIX / sPHENIX front-end CAD is **not public**; WREN + OpenIPMC + dense matrix boards are the open proxies for that stress class.",
        "",
        "---",
        "",
        "## Physical influences that drive human routing",
        "",
        "### 1. Electromagnetic environment",
        "",
        "- **Return path continuity:** High-speed edges (WR multi-Gb, Jetson memory, video) stay over continuous reference planes; layer changes use **via + nearby ground via** so loop inductance stays low.",
        "- **Impedance control:** Outer microstrip vs inner stripline — WREN/Jetson show substantial **inner copper fraction** when timing/SI dominates; audio `ecc83` stays **single-layer** to avoid via inductance and keep simple returns.",
        "- **Crosstalk / isolation:** Analog and RF (SatNOGS, ECC83, mixer) segregate by layer and geometry; digital buses accept tighter packing.",
        "",
        "### 2. Thermal and power integrity",
        "",
        "- **Plane-first power:** ATCA IPMC and SBC boards pour `GND`/`VCC` early; tracks are necks to pads. Zero extracted vias on some 6-layer boards often means **filled zones + thermal connections**, not “no layer changes.”",
        "- **Via farms under BGA:** Via density (vias per meter) spikes on Jetson/TinyTapeout — physics is **escape routing + current density**, not wirelength.",
        "",
        "### 3. Manufacturability (DFM)",
        "",
        "- Via size/drill and clearance floors (JLCPCB-class 0.15/0.60 rules) cap what an autorouter can legally place in 0402 rings (HALO lesson).",
        "- Human layouts often use **tighter fab capability** than generic autorouter defaults → completion cliffs under conservative rules.",
        "",
        "### 4. Topology before geometry",
        "",
        "- White Rabbit / multipin buses: humans assign **layer bands** (like HALO CPX F/In1/In2), then geometrize.",
        "- Autorouter policy **open > short** intentionally under-completes dense boards rather than emit illegal copper — charts of completion < 100% with hard_drc=0 are *honest*, not broken.",
        "",
        "---",
        "",
        "## Inventory table",
        "",
        f"**{len(present)}** boards with human copper extracted.",
        "",
        "| ID | Diff | Domain | Segs | Vias | Length mm | Nets | Cu layers | Outer frac | Via/m | Physics class |",
        "|----|------|--------|-----:|-----:|----------:|-----:|----------:|-----------:|------:|---------------|",
    ]
    for r in sorted(present, key=lambda x: -x.get("length_mm", 0)):
        lines.append(
            f"| `{r['id']}` | {r.get('difficulty')} | {r.get('domain')} | "
            f"{r.get('segments')} | {r.get('vias')} | {r.get('length_mm')} | "
            f"{r.get('nets_with_copper')} | {r.get('n_copper_layers_used')} | "
            f"{r.get('outer_fraction')} | {r.get('via_per_m')} | `{r.get('physics_class')}` |"
        )

    lines += [
        "",
        "## Charts — human routing solutions",
        "",
        "### Copper scale (length + vias)",
        "",
        "![human scale](images/golden/01_human_scale.png)",
        "",
        "Log scale makes WREN / Jetson / video dominate — those are the **SI + pin-escape** class. Small science boards sit two orders of magnitude lower.",
        "",
        "### Outer vs inner copper",
        "",
        "![layer strategy](images/golden/02_layer_strategy.png)",
        "",
        "- High **outer fraction**: simpler digital/tooling, hand-routed topside preference.",
        "- High **inner fraction**: stripline SI, EMC, or plane-adjacent critical nets (HEP timing, dense SBC).",
        "",
        "### Via density vs board scale",
        "",
        "![via density](images/golden/03_via_density.png)",
        "",
        "Upper-right (long + via-dense) = BGA escape / HDI-like. Lower-right (long, few vias) often = **zone-heavy power/GND** where connectivity is pours not drills.",
        "",
        "### Domain mass",
        "",
        "![domain mass](images/golden/04_domain_mass.png)",
        "",
        "---",
        "",
        "## Autorouter rip-and-reroute results",
        "",
    ]

    if routes:
        lines += [
            "| ID | Grade | Score | Completion | Hard DRC | AR L mm | Human L mm | AR vias | t (s) |",
            "|----|-------|------:|-----------:|---------:|--------:|-----------:|--------:|------:|",
        ]
        for r in routes:
            if r.get("error"):
                lines.append(
                    f"| `{r['id']}` | ERR | — | — | — | — | — | — | {r.get('time_s')} |"
                )
                continue
            lines.append(
                f"| `{r['id']}` | {r.get('golden_grade')} | {r.get('golden_score')} | "
                f"{r.get('completion_ratio')} | {r.get('hard_violations')} | "
                f"{r.get('ar_length_mm')} | {r.get('human_length_mm')} | "
                f"{r.get('ar_vias')} | {r.get('time_s')} |"
            )
        lines += [
            "",
            "### AR vs human length",
            "",
            "![ar length](images/golden/05_ar_vs_human_length.png)",
            "",
            "### Completion",
            "",
            "![completion](images/golden/06_ar_completion.png)",
            "",
            "### Time vs completion",
            "",
            "![time](images/golden/07_time_vs_completion.png)",
            "",
            "### Reading the AR charts physically",
            "",
            "1. **Completion < 1 with hard_drc=0** means the router refused illegal copper (correct under open>short).",
            "2. **Shorter AR length than human** on easy boards can mean fewer detours — or missing nets; always read with completion.",
            "3. **Via count ≠ quality**: humans may via-stitch grounds; AR may under-via and fail multipin connectivity.",
            "4. **Extreme boards** (WREN, Jetson) are inventory goldens for *human* topology metrics; full AR manufacturing gate is a research target, not a CI fail yet.",
            "",
        ]
    else:
        lines.append("_No route runs in this report. Re-run with `--route-easy`._")
        lines.append("")

    lines += [
        "---",
        "",
        "## Decision guide: what to optimize next",
        "",
        "| If chart shows… | Physical cause | Router work |",
        "|-----------------|----------------|-------------|",
        "| Low completion on multipin, 0 DRC | Congestion + pin access geometry | Conflict branching, shared escapes |",
        "| High human via/m, AR few vias | Escape/fanout under-modeled | Pin-access oracle + via size profile |",
        "| Human inner-heavy, AR outer-only | Layer assignment / via cost | Section layer plan + DSATUR + capacity mesh |",
        "| AR longer than human at full completion | Weak topology / rubberband | Topology-preserving consolidation |",
        "| Zone-heavy human, AR open power | Pours not tracks | Copper areas + KiCad refill connectivity |",
        "",
        "---",
        "",
        "## Sources & licenses",
        "",
        "See [examples/golden/SOURCES.md](../examples/golden/SOURCES.md). Boards under `third_party/golden/` are **not** vendored into git (see `.gitignore`); fetch with `scripts/fetch_golden_boards.sh`.",
        "",
        "Key upstreams: [OHWR](https://ohwr.org/), [WREN](https://ohwr.org/projects/wren/), KiCad `demos/vme-wren`, [OpenIPMC-HW](https://gitlab.com/openipmc/openipmc-hw), [SatNOGS COMMS](https://gitlab.com/librespacefoundation/satnogs-comms/satnogs-comms-hardware), [Antmicro Jetson baseboard](https://github.com/antmicro/jetson-nano-baseboard).",
        "",
        f"_Generated charts: {', '.join(Path(c).name for c in charts)}_",
        "",
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {REPORT}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--route-easy", action="store_true", help="Route easy+medium boards")
    ap.add_argument("--route-ids", default="", help="Comma-separated board ids to route")
    ap.add_argument("--effort", type=float, default=0.45)
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)

    print("=== Inventory (human extract) ===")
    inv = inventory()
    (OUT / "inventory.json").write_text(json.dumps(inv, indent=2) + "\n")

    routes: list[dict[str, Any]] = []
    ids: list[str] = []
    if args.route_ids:
        ids = [x.strip() for x in args.route_ids.split(",") if x.strip()]
    elif args.route_easy:
        # Keep default route set small: soft timeout does not cancel native search.
        ids = [
            r["id"]
            for r in inv
            if not r.get("missing")
            and DIFFICULTY.get(r["id"]) == "easy"
            and r.get("segments", 0) < 800
            and r.get("nets_with_copper", 0) < 80
        ]
    if ids:
        print("=== Route", ids, "===")
        for bid in ids:
            print(f"  routing {bid}…")
            row = route_board(bid, effort=args.effort, timeout_s=args.timeout)
            routes.append(row)
            print(
                f"    -> grade={row.get('golden_grade')} completion={row.get('completion_ratio')} "
                f"t={row.get('time_s')}s err={row.get('error')}"
            )
        (OUT / "route_results.json").write_text(json.dumps(routes, indent=2) + "\n")

    print("=== Charts ===")
    charts = make_charts(inv, routes)
    for c in charts:
        print(" ", c)
    write_report(inv, routes, charts)
    print("Done.")


if __name__ == "__main__":
    main()
