#!/usr/bin/env python3
"""Run golden-eval on CERN-OHL / open-hardware example boards and render gallery.

  python scripts/run_ohl_golden_gallery.py
  python scripts/run_ohl_golden_gallery.py --extract-only
  python scripts/run_ohl_golden_gallery.py --ids pq9_devboard,ecc83_pp

Writes:
  docs/images/golden/ohl_*.png
  viewer/runs/ohl_gallery/suite_results.json
  examples/golden/RESULTS.md  (table + image links for README)
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "viewer" / "runs" / "ohl_gallery"
IMG = ROOT / "docs" / "images" / "golden"
RESULTS_MD = ROOT / "examples" / "golden" / "RESULTS.md"

# Open Hardware License / public demo boards (paths relative to repo)
OHL_BOARDS: list[dict[str, Any]] = [
    {
        "id": "simple_2net",
        "pcb": "tests/fixtures/golden/simple_2net.kicad_pcb",
        "license": "MIT (fixture)",
        "difficulty": "easy",
        "expect": "manufacturing_gate",
        "timeout_s": 60,
        "min_completion": 1.0,
    },
    {
        "id": "openflexure_illum",
        "pcb": "third_party/golden/openflexure-illum/SimpleIllumination.kicad_pcb",
        "license": "CERN-OHL-S",
        "difficulty": "easy",
        "expect": "partial_ok",
        "timeout_s": 60,
        "min_completion": 0.0,
    },
    {
        "id": "ofm_illumination",
        "pcb": "third_party/golden/ofm-led/ofm_cc_illumination.kicad_pcb",
        "license": "CERN-OHL",
        "difficulty": "easy",
        "expect": "partial_ok",
        "timeout_s": 60,
        "min_completion": 0.0,
    },
    {
        "id": "pq9_devboard",
        "pcb": "third_party/golden/pq9-devboard/pq9-devboard.kicad_pcb",
        "license": "CERN-OHL",
        "difficulty": "easy",
        "expect": "partial_ok",
        "timeout_s": 90,
        "min_completion": 0.0,
    },
    {
        "id": "ecc83_pp",
        "pcb": "third_party/golden/kicad-demos/ecc83/ecc83-pp.kicad_pcb",
        "license": "KiCad demo",
        "difficulty": "easy",
        "expect": "partial_ok",
        "timeout_s": 60,
        "min_completion": 0.0,
    },
    {
        "id": "ecc83_pp_v2",
        "pcb": "third_party/golden/kicad-demos/ecc83/ecc83-pp_v2.kicad_pcb",
        "license": "KiCad demo",
        "difficulty": "easy",
        "expect": "partial_ok",
        "timeout_s": 60,
        "min_completion": 0.0,
    },
    {
        "id": "complex_hierarchy",
        "pcb": "third_party/golden/kicad-demos/complex_hierarchy/complex_hierarchy.kicad_pcb",
        "license": "KiCad demo",
        "difficulty": "easy",
        "expect": "partial_ok",
        "timeout_s": 90,
        "min_completion": 0.0,
    },
    {
        "id": "sonde_xilinx",
        "pcb": "third_party/golden/kicad-demos/sonde xilinx/sonde xilinx.kicad_pcb",
        "license": "KiCad demo",
        "difficulty": "easy",
        "expect": "partial_ok",
        "timeout_s": 90,
        "min_completion": 0.0,
    },
    {
        "id": "pic_programmer",
        "pcb": "third_party/golden/kicad-demos/pic_programmer/pic_programmer.kicad_pcb",
        "license": "KiCad demo",
        "difficulty": "medium",
        "expect": "partial_ok",
        "timeout_s": 120,
        "min_completion": 0.0,
    },
    {
        "id": "multichannel_mixer",
        "pcb": "third_party/golden/kicad-demos/multichannel/multichannel_mixer.kicad_pcb",
        "license": "KiCad demo",
        "difficulty": "medium",
        "expect": "partial_ok",
        "timeout_s": 120,
        "min_completion": 0.0,
    },
    # Harder OHL — extract + short deadline route
    {
        "id": "openipmc_hw",
        "pcb": "third_party/golden/openipmc-hw/openipmc-hw.kicad_pcb",
        "license": "open (OpenIPMC)",
        "difficulty": "hard",
        "expect": "partial_ok",
        "timeout_s": 180,
        "min_completion": 0.0,
    },
    {
        "id": "satnogs_comms",
        "pcb": "third_party/golden/satnogs-comms/satnogs-comms.kicad_pcb",
        "license": "CERN-OHL",
        "difficulty": "hard",
        "expect": "partial_ok",
        "timeout_s": 180,
        "min_completion": 0.0,
    },
]


def _render_board_pair(
    human_json: Path,
    ar_json: Path | None,
    out_png: Path,
    title: str,
) -> None:
    """Side-by-side human vs AR copper scatter/line plot."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def load_segs(path: Path) -> list[dict]:
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("segments") or [])

    def load_vias(path: Path) -> list[dict]:
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("vias") or [])

    h_segs = load_segs(human_json)
    a_segs = load_segs(ar_json) if ar_json else []
    h_vias = load_vias(human_json)
    a_vias = load_vias(ar_json) if ar_json else []

    fig, axes = plt.subplots(1, 2, figsize=(11, 5.2))
    for ax, segs, vias, name, color in (
        (axes[0], h_segs, h_vias, "Human (original)", "#64748b"),
        (axes[1], a_segs, a_vias, "Autorouter", "#2563eb"),
    ):
        for s in segs:
            ax.plot(
                [s["x1"], s["x2"]],
                [s["y1"], s["y2"]],
                color=color,
                linewidth=max(0.4, float(s.get("width_mm") or 0.2) * 2.5),
                alpha=0.85,
                solid_capstyle="round",
            )
        if vias:
            ax.scatter(
                [v["x"] for v in vias],
                [v["y"] for v in vias],
                s=12,
                c="#ef4444",
                zorder=5,
                label="vias",
            )
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_title(f"{name}\nsegs={len(segs)} vias={len(vias)}")
        ax.set_xlabel("mm")
        ax.set_ylabel("mm")
        ax.grid(True, alpha=0.25)
        ax.invert_yaxis()  # KiCad-like
    fig.suptitle(title, fontsize=12, fontweight="bold")
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=130)
    plt.close(fig)


def _render_scoreboard(rows: list[dict[str, Any]], out_png: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    ok = [r for r in rows if r.get("completion_ratio") is not None and not r.get("skipped")]
    if not ok:
        return
    ok = sorted(ok, key=lambda r: float(r.get("completion_ratio") or 0))
    ids = [r["id"] for r in ok]
    comp = [100 * float(r.get("completion_ratio") or 0) for r in ok]
    scores = [float(r.get("golden_score") or 0) for r in ok]
    x = np.arange(len(ids))
    fig, ax = plt.subplots(figsize=(11, 5))
    w = 0.38
    ax.bar(x - w / 2, comp, w, label="Completion % vs human", color="#22c55e")
    ax.bar(x + w / 2, scores, w, label="Golden score /100", color="#3b82f6")
    ax.set_xticks(x)
    ax.set_xticklabels(ids, rotation=40, ha="right", fontsize=8)
    ax.set_ylim(0, 110)
    ax.set_ylabel("%")
    ax.set_title("OHL / open-hardware golden suite — AR vs human routing")
    ax.legend(loc="upper left")
    ax.axhline(100, color="#94a3b8", linestyle="--", linewidth=0.8)
    # annotate grades
    for i, r in enumerate(ok):
        g = r.get("golden_grade") or ""
        ax.text(i, max(comp[i], scores[i]) + 2, g, ha="center", fontsize=8)
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=140)
    plt.close(fig)


def _render_length_compare(rows: list[dict[str, Any]], out_png: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    ok = [
        r
        for r in rows
        if r.get("ar") and r.get("human") and not r.get("skipped") and not r.get("error")
    ]
    if not ok:
        return
    ids = [r["id"] for r in ok]
    h_l = [float((r.get("human") or {}).get("length_mm") or 0) for r in ok]
    a_l = [float((r.get("ar") or {}).get("length_mm") or 0) for r in ok]
    x = np.arange(len(ids))
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.bar(x - 0.2, h_l, 0.4, label="Human length (mm)", color="#64748b")
    ax.bar(x + 0.2, a_l, 0.4, label="AR length (mm)", color="#2563eb")
    ax.set_xticks(x)
    ax.set_xticklabels(ids, rotation=40, ha="right", fontsize=8)
    ax.set_ylabel("Track length (mm)")
    ax.set_title("Track length: human original vs autorouter")
    ax.legend()
    # log if span is large
    if max(h_l + a_l) / max(1e-6, min(v for v in h_l + a_l if v > 0) or 1) > 50:
        ax.set_yscale("log")
    fig.tight_layout()
    fig.savefig(out_png, dpi=140)
    plt.close(fig)


def run_boards(
    *,
    extract_only: bool,
    ids: list[str] | None,
    effort: float,
    hard_deadline: bool,
) -> list[dict[str, Any]]:
    from physics_router.golden_eval import evaluate_board

    OUT.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for entry in OHL_BOARDS:
        bid = entry["id"]
        if ids is not None and bid not in ids:
            continue
        pcb = ROOT / entry["pcb"]
        if not pcb.is_file():
            rows.append(
                {
                    "id": bid,
                    "skipped": True,
                    "error": f"missing {entry['pcb']}",
                    "license": entry.get("license"),
                }
            )
            print(f"  SKIP {bid}: pcb missing")
            continue
        e = dict(entry)
        e["_base"] = str(ROOT)
        e["cbs_repair"] = entry.get("difficulty") == "easy"
        print(f"  RUN  {bid} (timeout={e.get('timeout_s')}s)…")
        t0 = time.time()
        row = evaluate_board(
            e,
            pipeline="capacity",
            effort=effort,
            out_dir=OUT / bid,
            extract_only=extract_only,
            hard_deadline=hard_deadline,
            cbs_repair=bool(e.get("cbs_repair")),
        )
        row["license"] = entry.get("license")
        row["wall_s"] = round(time.time() - t0, 2)
        rows.append(row)
        status = (
            "SKIP"
            if row.get("skipped")
            else ("TIMEOUT" if row.get("timed_out") else ("PASS" if row.get("passed") else "FAIL"))
        )
        print(
            f"       {status} grade={row.get('golden_grade')} "
            f"completion={row.get('completion_ratio')} "
            f"hard_drc={row.get('hard_violations')} t={row.get('time_s')}s"
        )
    return rows


def make_images(rows: list[dict[str, Any]]) -> list[str]:
    IMG.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    # Per-board human vs AR
    for r in rows:
        if r.get("skipped") or r.get("error") and not (OUT / r["id"] / "human_route.json").is_file():
            continue
        bid = r["id"]
        human = OUT / bid / "human_route.json"
        ar = OUT / bid / "ar_route.json"
        if not human.is_file():
            continue
        png = IMG / f"ohl_{bid}_compare.png"
        _render_board_pair(
            human,
            ar if ar.is_file() else None,
            png,
            f"{bid} — human vs autorouter copper",
        )
        paths.append(str(png.relative_to(ROOT)))
        print(f"  img {png.name}")

    score_png = IMG / "ohl_scoreboard.png"
    _render_scoreboard(rows, score_png)
    paths.append(str(score_png.relative_to(ROOT)))
    len_png = IMG / "ohl_length_compare.png"
    _render_length_compare(rows, len_png)
    paths.append(str(len_png.relative_to(ROOT)))
    return paths


def write_results_md(rows: list[dict[str, Any]], images: list[str]) -> None:
    lines = [
        "# OHL / open-hardware golden results",
        "",
        "Rip-and-reroute scores against **original human copper** on CERN-OHL and public demo boards.",
        "",
        "Policy: open nets beat shorts; hard DRC must stay 0 on committed copper.",
        "",
        f"Generated by `scripts/run_ohl_golden_gallery.py`. Suite dir: `viewer/runs/ohl_gallery/`.",
        "",
        "## Scoreboard",
        "",
        "![scoreboard](../../docs/images/golden/ohl_scoreboard.png)",
        "",
        "![length](../../docs/images/golden/ohl_length_compare.png)",
        "",
        "## Table",
        "",
        "| Board | License | Diff | Grade | Score | Completion | Hard DRC | AR L mm | Human L mm | AR vias | Human vias | t (s) | Status |",
        "|-------|---------|------|-------|------:|-----------:|---------:|--------:|-----------:|--------:|-----------:|------:|:------:|",
    ]
    for r in rows:
        if r.get("skipped"):
            lines.append(
                f"| `{r['id']}` | {r.get('license','')} | — | — | — | — | — | — | — | — | — | — | skip |"
            )
            continue
        h = r.get("human") or {}
        a = r.get("ar") or {}
        status = "TIMEOUT" if r.get("timed_out") else ("PASS" if r.get("passed") else "FAIL")
        if r.get("error") and not a:
            status = "ERR"
        lines.append(
            f"| `{r['id']}` | {r.get('license','')} | {r.get('difficulty','')} | "
            f"{r.get('golden_grade') or '—'} | {r.get('golden_score') if r.get('golden_score') is not None else '—'} | "
            f"{r.get('completion_ratio') if r.get('completion_ratio') is not None else '—'} | "
            f"{r.get('hard_violations') if r.get('hard_violations') is not None else '—'} | "
            f"{a.get('length_mm', '—')} | {h.get('length_mm', '—')} | "
            f"{a.get('vias', '—')} | {h.get('vias', '—')} | "
            f"{r.get('time_s', '—')} | {status} |"
        )

    lines += [
        "",
        "## Per-board copper (human left · AR right)",
        "",
    ]
    for r in rows:
        bid = r["id"]
        rel = f"../../docs/images/golden/ohl_{bid}_compare.png"
        png = IMG / f"ohl_{bid}_compare.png"
        if png.is_file():
            lines.append(f"### `{bid}`")
            lines.append("")
            lines.append(f"![{bid}]({rel})")
            lines.append("")
            missing = r.get("missing_nets") or []
            if missing:
                lines.append(f"Missing vs human: `{', '.join(missing[:16])}`")
                lines.append("")

    lines += [
        "## Notes",
        "",
        "- **Completion** = fraction of human copper nets the AR fully committed.",
        "- **Shorter AR length** with completion < 1 is not “better” — nets may be open.",
        "- Hard boards (OpenIPMC, SatNOGS) use hard deadlines; TIMEOUT is honest.",
        "- Fetch boards: `bash scripts/fetch_golden_boards.sh`",
        "",
    ]
    RESULTS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {RESULTS_MD}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--extract-only", action="store_true")
    ap.add_argument("--ids", default="", help="Comma-separated board ids")
    ap.add_argument("--effort", type=float, default=0.45)
    ap.add_argument("--soft-timeout", action="store_true", help="Disable hard process kill")
    args = ap.parse_args()
    ids = [x.strip() for x in args.ids.split(",") if x.strip()] or None

    print("=== OHL golden gallery ===")
    rows = run_boards(
        extract_only=args.extract_only,
        ids=ids,
        effort=args.effort,
        hard_deadline=not args.soft_timeout,
    )
    (OUT / "suite_results.json").write_text(
        json.dumps({"boards": rows}, indent=2) + "\n", encoding="utf-8"
    )
    print("=== Images ===")
    images = make_images(rows)
    write_results_md(rows, images)
    n_pass = sum(1 for r in rows if r.get("passed") and not r.get("skipped"))
    n_fail = sum(1 for r in rows if not r.get("passed") and not r.get("skipped"))
    print(f"Done: {n_pass} pass · {n_fail} fail/timeout · images={len(images)}")


if __name__ == "__main__":
    main()
