#!/usr/bin/env python3
"""Render documentation figures for the TopoR-style routing process.

Produces PNGs under docs/images/routing_process/ showing pipeline stages:
  1_placement_outline, 2_guide_topology, 3_clearance_raw,
  4_regeometry, 5_by_layer, 6_process_strip, 7_drc_map
and machine-readable render_meta.json / drc_report.json.

Usage (repo root, venv active, native built):
  python scripts/render_routing_process.py
  python scripts/render_routing_process.py --halo   # also try HALO-90 if present
  python scripts/render_routing_process.py --halo-only --pcb board.kicad_pcb
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Circle, Polygon, Rectangle  # noqa: E402

from physics_router.config_io import example_config, load_config
from physics_router.design_rules import default_design_rules, load_design_rules
from physics_router.graph_theory import plan_graph_topology
from physics_router.kicad_io import board_from_synthetic, load_board_from_kicad_pcb
from physics_router.native_bridge import info as native_info
from physics_router.regeometry import (
    compute_topor_geometry_metrics,
    post_connect_regeometry,
)
from physics_router.router import (
    clearance_aware_route,
    native_drc_check,
    outline_polygon_from_board,
    topological_guide_route,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images" / "routing_process"
PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
CFG = ROOT / "examples/halo-90/placement_config.yaml"

LAYER_COLORS = {
    "F.Cu": "#C83434",
    "B.Cu": "#3A6BC8",
    "In1.Cu": "#4CA64C",
    "In2.Cu": "#C87A28",
}


def net_color(cfg, name: str) -> str:
    if cfg is None:
        return "#555555"
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


def _limits(board, pad: float = 2.0):
    xs = [c.x_mm for c in board.components.values()]
    ys = [c.y_mm for c in board.components.values()]
    poly = outline_polygon_from_board(board)
    if poly:
        xs.extend(p[0] for p in poly)
        ys.extend(p[1] for p in poly)
    return (min(xs) - pad, max(xs) + pad), (min(ys) - pad, max(ys) + pad)


def _draw_board_base(ax, board, cfg, *, show_outline=True, dim_parts=False):
    xlim, ylim = _limits(board)
    ax.set_aspect("equal")
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    poly = outline_polygon_from_board(board)
    if show_outline and poly:
        ax.add_patch(
            Polygon(
                poly,
                closed=True,
                fill=True,
                facecolor="#143726",
                edgecolor="#D0D8E0",
                linewidth=1.2,
                alpha=0.9,
            )
        )
    elif show_outline:
        # fallback circle for center-origin round boards
        ax.add_patch(
            Circle(
                (0, 0),
                12,
                fill=True,
                facecolor="#143726",
                edgecolor="#D0D8E0",
                alpha=0.9,
            )
        )
    for ref, c in board.components.items():
        w, h = max(c.width_mm, 0.35), max(c.height_mm, 0.35)
        if dim_parts:
            fc, ec, al = "#2a3a44", "#667788", 0.7
        else:
            fc, ec, al = "#3a3a50", "#e8d060", 0.9
            if ref.startswith("D") and ref[1:].isdigit():
                fc = "#a04040"
        ax.add_patch(
            Rectangle(
                (c.x_mm - w / 2, c.y_mm - h / 2),
                w,
                h,
                facecolor=fc,
                edgecolor=ec,
                linewidth=0.35,
                alpha=al,
            )
        )
        if not (ref.startswith("D") and ref[1:].isdigit()) and not dim_parts:
            ax.text(
                c.x_mm, c.y_mm, ref, fontsize=5, ha="center", va="center", color="#eee"
            )
    ax.set_facecolor("#0a1020")
    ax.tick_params(colors="#8899aa", labelsize=7)
    ax.xaxis.label.set_color("#8899aa")
    ax.yaxis.label.set_color("#8899aa")
    ax.title.set_color("#e8eef7")
    for spine in ax.spines.values():
        spine.set_color("#2a3548")
    ax.grid(True, alpha=0.15, color="#445566")


def _draw_route(ax, route, cfg, *, alpha=0.9, by_layer=False):
    for area in route.areas:
        if len(area.outline) < 3:
            continue
        color = LAYER_COLORS.get(area.layer, "#aaaaaa")
        ax.add_patch(
            Polygon(
                area.outline,
                closed=True,
                facecolor=color,
                edgecolor=color,
                linewidth=0.9,
                alpha=0.24,
                zorder=2,
            )
        )
    for seg in route.segments:
        if by_layer:
            col = LAYER_COLORS.get(seg.layer, "#aaaaaa")
        else:
            col = net_color(cfg, seg.net)
        lw = max(0.8, min(4.0, seg.width_mm * 8))
        ax.plot(
            [seg.x1, seg.x2],
            [seg.y1, seg.y2],
            color=col,
            linewidth=lw,
            alpha=alpha,
            solid_capstyle="round",
            zorder=3,
        )
    for v in route.vias or []:
        ax.plot(
            v.x,
            v.y,
            "o",
            color="#c0a060",
            markersize=4,
            markeredgecolor="#222",
            zorder=4,
        )


def _metrics_box(ax, text: str):
    ax.text(
        0.02,
        0.98,
        text,
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=7,
        color="#e8eef7",
        family="monospace",
        bbox=dict(
            boxstyle="round,pad=0.35",
            facecolor="#0a1020",
            edgecolor="#5b9fd4",
            alpha=0.88,
        ),
    )


def render_suite(board, cfg, rules, tag: str) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    meta: dict = {
        "tag": tag,
        "native": native_info(),
        "timings_s": {},
        "metrics": {},
    }
    n_comp = len(board.components)
    n_nets = len(board.nets)

    # --- 1 Placement + outline ---
    fig, ax = plt.subplots(figsize=(7.2, 7.2), dpi=140)
    _draw_board_base(ax, board, cfg, show_outline=True, dim_parts=False)
    poly = outline_polygon_from_board(board)
    title = f"1 · Placement + Edge.Cuts outline — {tag}"
    ax.set_title(title, fontsize=11)
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    n_outline = len(poly) if poly else 0
    _metrics_box(
        ax,
        f"components={n_comp}\nnets={n_nets}\noutline_pts={n_outline}\n"
        f"layers={list(board.copper_layers)}",
    )
    fig.tight_layout()
    p1 = OUT / f"{tag}_1_placement_outline.png"
    fig.savefig(p1, bbox_inches="tight", facecolor="#0a1020")
    plt.close(fig)

    # --- 2 Guide topology ---
    t0 = time.perf_counter()
    guide = topological_guide_route(board, cfg)
    meta["timings_s"]["guide"] = round(time.perf_counter() - t0, 3)
    graph_plan = plan_graph_topology(board, cfg).to_dict()
    fig, ax = plt.subplots(figsize=(7.2, 7.2), dpi=140)
    _draw_board_base(ax, board, cfg, show_outline=True, dim_parts=True)
    _draw_route(ax, guide, cfg, alpha=0.85)
    ax.set_title(
        f"2 · Hypergraph guide — crossing-aware MST + DSATUR layers\n"
        f"{len(guide.segments)} segs · {guide.total_length_mm:.1f} mm",
        fontsize=10,
    )
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    _metrics_box(
        ax,
        f"segs={len(guide.segments)}\nlength={guide.total_length_mm:.1f}mm\n"
        f"vias={guide.via_count}\nunrouted={len(guide.unrouted_nets)}",
    )
    fig.tight_layout()
    p2 = OUT / f"{tag}_2_guide_topology.png"
    fig.savefig(p2, bbox_inches="tight", facecolor="#0a1020")
    plt.close(fig)

    # --- 3 Clearance raw (no regeometry) ---
    cl = 0.2
    if rules is not None:
        cl = max(0.15, rules.constraints.min_clearance_mm)
    t0 = time.perf_counter()
    raw = clearance_aware_route(
        board,
        cfg,
        layers=list(board.copper_layers) or ["F.Cu", "B.Cu"],
        clearance_mm=cl,
        grid_mm=0.5,
        soft_fallback=False,
        prefer_native=True,
        allow_vias=True,
        style="hybrid",
        design_rules=rules,
    )
    meta["timings_s"]["clearance_raw"] = round(time.perf_counter() - t0, 3)
    route_graph = (raw.quality or {}).get("graph_topology", {})
    m_raw = compute_topor_geometry_metrics(raw)
    fig, ax = plt.subplots(figsize=(7.2, 7.2), dpi=140)
    _draw_board_base(ax, board, cfg, show_outline=True, dim_parts=True)
    _draw_route(ax, raw, cfg, alpha=0.9)
    ax.set_title(
        "3 · Native hybrid free-angle (raw connectivity)\n"
        "power → critical → parallel matrix bundle → general",
        fontsize=10,
    )
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    sp = m_raw.min_spacing_mm
    sp_s = f"{sp:.2f}" if sp < 900 else "n/a"
    _metrics_box(
        ax,
        f"segs={m_raw.segment_count}\nbends={m_raw.bend_count}\n"
        f"vias={m_raw.via_count}\nlength={m_raw.total_length_mm:.1f}mm\n"
        f"min_spacing={sp_s}mm\nunrouted={len(raw.unrouted_nets)}",
    )
    fig.tight_layout()
    p3 = OUT / f"{tag}_3_clearance_raw.png"
    fig.savefig(p3, bbox_inches="tight", facecolor="#0a1020")
    plt.close(fig)

    # --- 4 Post-connect re-geometry ---
    t0 = time.perf_counter()
    polished = post_connect_regeometry(
        raw, board, clearance_mm=cl, iterations=14, use_arcs=True, max_seg_mm=2.5
    )
    raw_drc = native_drc_check(raw, clearance_mm=cl, board=board)
    polished_drc = native_drc_check(polished, clearance_mm=cl, board=board)
    if polished_drc["violations"] > raw_drc["violations"]:
        polished = raw
        polished.notes.append(
            "docs render: reverted regeometry because exact DRC worsened"
        )
    meta["timings_s"]["regeometry"] = round(time.perf_counter() - t0, 3)
    m_pol = compute_topor_geometry_metrics(polished)
    tg = (polished.quality or {}).get("topor_geometry") or m_pol.to_dict()
    fig, ax = plt.subplots(figsize=(7.2, 7.2), dpi=140)
    _draw_board_base(ax, board, cfg, show_outline=True, dim_parts=True)
    _draw_route(ax, polished, cfg, alpha=0.92)
    ax.set_title(
        "4 · Post-connect re-geometry (spacing + multi-bend + arcs)\n"
        "subdivide → repel → arc chords",
        fontsize=10,
    )
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    _metrics_box(
        ax,
        f"segs={tg.get('segment_count', m_pol.segment_count)}\n"
        f"bends={tg.get('bend_count', m_pol.bend_count)}\n"
        f"arcs={tg.get('arc_corners', 0)}\n"
        f"multi_bend_nets={tg.get('multi_bend_nets', m_pol.multi_bend_nets)}\n"
        f"vias={tg.get('via_count', m_pol.via_count)}\n"
        f"length={float(tg.get('total_length_mm', m_pol.total_length_mm)):.1f}mm",
    )
    fig.tight_layout()
    p4 = OUT / f"{tag}_4_regeometry.png"
    fig.savefig(p4, bbox_inches="tight", facecolor="#0a1020")
    plt.close(fig)

    # --- 5 By layer ---
    layers = (
        sorted(
            {s.layer for s in polished.segments}
            | {area.layer for area in polished.areas}
        )
        or list(board.copper_layers)
        or ["F.Cu"]
    )
    n = max(1, len(layers))
    fig, axes = plt.subplots(1, n, figsize=(3.6 * n, 3.8), dpi=140)
    if n == 1:
        axes = [axes]
    for ax, ly in zip(axes, layers):
        _draw_board_base(ax, board, cfg, show_outline=True, dim_parts=True)
        for area in polished.areas:
            if area.layer == ly and len(area.outline) >= 3:
                color = LAYER_COLORS.get(ly, "#aaa")
                ax.add_patch(
                    Polygon(
                        area.outline,
                        closed=True,
                        facecolor=color,
                        edgecolor=color,
                        linewidth=0.8,
                        alpha=0.28,
                    )
                )
        segs = [s for s in polished.segments if s.layer == ly]
        for seg in segs:
            col = LAYER_COLORS.get(ly, "#aaa")
            ax.plot(
                [seg.x1, seg.x2],
                [seg.y1, seg.y2],
                color=col,
                linewidth=max(1.0, seg.width_mm * 7),
                alpha=0.9,
                solid_capstyle="round",
            )
        for v in polished.vias or []:
            ax.plot(v.x, v.y, "o", color="#c0a060", markersize=3.5, zorder=4)
        area_count = sum(1 for area in polished.areas if area.layer == ly)
        ax.set_title(
            f"{ly} · {len(segs)} segs · {area_count} areas",
            fontsize=9,
            color="#e8eef7",
        )
        ax.set_xlabel("x (mm)", fontsize=7)
        ax.set_ylabel("y (mm)", fontsize=7)
    fig.suptitle(f"5 · Copper by layer — {tag}", color="#e8eef7", fontsize=11)
    fig.patch.set_facecolor("#0a1020")
    fig.tight_layout()
    p5 = OUT / f"{tag}_5_by_layer.png"
    fig.savefig(p5, bbox_inches="tight", facecolor="#0a1020")
    plt.close(fig)

    # --- 6 Process strip (montage of 1–4) ---
    fig, axes = plt.subplots(1, 4, figsize=(14, 3.8), dpi=130)
    fig.patch.set_facecolor("#0a1020")
    stages = [
        (p1, "1 Placement\n+ outline"),
        (p2, "2 Guide\ntopology"),
        (p3, "3 Clearance\nraw"),
        (p4, "4 Re-geometry\nbends+arcs"),
    ]
    for ax, (path, label) in zip(axes, stages):
        img = plt.imread(path)
        ax.imshow(img)
        ax.set_title(label, fontsize=9, color="#e8eef7")
        ax.axis("off")
    fig.suptitle(
        f"TopoR-style routing process — {tag}",
        color="#e8eef7",
        fontsize=12,
        y=1.02,
    )
    fig.tight_layout()
    p6 = OUT / f"{tag}_6_process_strip.png"
    fig.savefig(p6, bbox_inches="tight", facecolor="#0a1020")
    plt.close(fig)

    # --- 7 Exact native DRC map ---
    drc = native_drc_check(polished, clearance_mm=cl, board=board)
    fig, ax = plt.subplots(figsize=(7.2, 7.2), dpi=140)
    _draw_board_base(ax, board, cfg, show_outline=True, dim_parts=True)
    _draw_route(ax, polished, cfg, alpha=0.9, by_layer=True)
    marker_colors = {"short": "#ff3344", "spacing": "#ff9f32", "outline": "#ff55cc"}
    for item in drc.get("items") or []:
        ax.plot(
            item["x"],
            item["y"],
            marker="x",
            color=marker_colors.get(item["kind"], "#ffffff"),
            markersize=6,
            markeredgewidth=1.2,
            zorder=6,
        )
    ax.set_title(
        f"7 · Exact native DRC — {tag}\n"
        f"{drc['violations']} violations · {len(polished.unrouted_nets)} open nets",
        fontsize=10,
    )
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    _metrics_box(
        ax,
        f"shorts={drc['shorts']}\nspacing={drc['spacing']}\n"
        f"outside={drc['outline_outside']}\nareas={len(polished.areas)}\n"
        f"unrouted={len(polished.unrouted_nets)}",
    )
    fig.tight_layout()
    p7 = OUT / f"{tag}_7_drc_map.png"
    fig.savefig(p7, bbox_inches="tight", facecolor="#0a1020")
    plt.close(fig)

    # Canonical aliases for README (prefer synthetic if both, overwrite with last)
    for src, name in (
        (p1, "1_placement_outline.png"),
        (p2, "2_guide_topology.png"),
        (p3, "3_clearance_raw.png"),
        (p4, "4_regeometry.png"),
        (p5, "5_by_layer.png"),
        (p6, "6_process_strip.png"),
        (p7, "7_drc_map.png"),
    ):
        dest = OUT / name
        dest.write_bytes(src.read_bytes())
    (OUT / f"{tag}_drc_map.png").write_bytes(p7.read_bytes())

    meta["metrics"] = {
        "graph_plan": graph_plan,
        "route_graph": route_graph,
        "raw": m_raw.to_dict(),
        "regeometry": tg if isinstance(tg, dict) else m_pol.to_dict(),
        "guide_segs": len(guide.segments),
        "polished_segs": len(polished.segments),
    }
    meta["drc"] = {
        key: drc[key]
        for key in (
            "violations",
            "shorts",
            "spacing",
            "outline_outside",
            "area_outside",
            "areas_deferred_to_kicad",
        )
    }
    meta["route"] = {
        "segments": len(polished.segments),
        "vias": len(polished.vias),
        "areas": len(polished.areas),
        "length_mm": round(polished.total_length_mm, 3),
        "unrouted_nets": list(polished.unrouted_nets),
        "net_status": {report.net: report.status for report in polished.net_reports},
    }
    meta["images"] = [
        p1.name,
        p2.name,
        p3.name,
        p4.name,
        p5.name,
        p6.name,
        p7.name,
    ]
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--halo", action="store_true", help="Also render HALO-90 if PCB is present"
    )
    ap.add_argument("--halo-only", action="store_true", help="Only HALO-90")
    ap.add_argument("--pcb", type=Path, default=PCB, help="KiCad PCB for HALO render")
    ap.add_argument("--config", type=Path, default=CFG, help="Placement config")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    all_meta: dict = {}

    if not args.halo_only:
        print("Rendering synthetic demo board…")
        cfg = example_config()
        board = board_from_synthetic(cfg)
        # Synthetic has no Edge.Cuts — add a generous circle so outline renders
        if not board.outline:
            r = max(board.width_mm, board.height_mm) * 0.55
            cx, cy = board.width_mm / 2, board.height_mm / 2
            n = 48
            pts = [
                [
                    cx + r * math.cos(2 * math.pi * i / n),
                    cy + r * math.sin(2 * math.pi * i / n),
                ]
                for i in range(n)
            ]
            board.outline = [
                {"kind": "circle", "cx": cx, "cy": cy, "r": r, "layer": "Edge.Cuts"},
                {"kind": "poly", "pts": pts, "closed": True, "layer": "Edge.Cuts"},
            ]
        meta = render_suite(board, cfg, default_design_rules(), "synthetic")
        all_meta["synthetic"] = meta
        print("  timings", meta["timings_s"])

    if args.halo or args.halo_only:
        if not args.pcb.exists():
            print("HALO-90 PCB not found; skip --halo")
        else:
            print("Rendering HALO-90 (faster grid; may take a minute)…")
            cfg = load_config(args.config)
            board = load_board_from_kicad_pcb(args.pcb, cfg)
            rules = load_design_rules(args.pcb)
            # Faster path for docs: clearance route only (full topor is slow)
            meta = render_suite(board, cfg, rules, "halo90")
            all_meta["halo90"] = meta
            print("  timings", meta["timings_s"])

    (OUT / "render_meta.json").write_text(json.dumps(all_meta, indent=2) + "\n")
    if "halo90" in all_meta:
        (OUT / "drc_report.json").write_text(
            json.dumps(all_meta["halo90"], indent=2) + "\n"
        )
    print("Wrote images to", OUT)
    for p in sorted(OUT.glob("*.png")):
        print(" ", p.name, f"{p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
