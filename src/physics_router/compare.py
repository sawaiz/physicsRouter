"""Compare route results: FreeRouting baselines and human golden copper."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from physics_router.router import RouteResult


def load_route_metrics(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return metrics_from_route_dict(data, source=str(path))


def metrics_from_route_dict(data: dict[str, Any], *, source: str = "") -> dict[str, Any]:
    segs = data.get("segments") or []
    return {
        "source": source,
        "total_length_mm": float(data.get("total_length_mm") or 0),
        "via_count": int(data.get("via_count") or len(data.get("vias") or [])),
        "segments": len(segs) if isinstance(segs, list) else int(segs or 0),
        "unrouted_nets": list(data.get("unrouted_nets") or []),
        "clearance_violations": int(data.get("clearance_violations") or 0),
        "notes": list(data.get("notes") or [])[:12],
        "length_by_layer_mm": dict(data.get("length_by_layer_mm") or {}),
        "net_reports": list(data.get("net_reports") or []),
    }


def metrics_from_route_result(result: RouteResult, *, source: str = "") -> dict[str, Any]:
    """Aggregate metrics from a live :class:`RouteResult`."""
    d = result.to_dict()
    return metrics_from_route_dict(d, source=source or "route_result")


def parse_ses_metrics(ses_path: str | Path) -> dict[str, Any]:
    """Best-effort metrics from a Specctra SES session file."""
    text = Path(ses_path).read_text(encoding="utf-8", errors="replace")
    wires = len(re.findall(r"\(wire\b", text))
    vias = len(re.findall(r"\(via\b", text))
    # path lengths are rarely explicit; count path vertices as proxy
    paths = re.findall(r"\(path\s+\S+\s+([\d.\s]+)\)", text)
    length_mil = 0.0
    for p in paths:
        nums = [float(x) for x in p.split() if _is_float(x)]
        coords = list(zip(nums[0::2], nums[1::2]))
        for a, b in zip(coords, coords[1:]):
            length_mil += ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    length_mm = length_mil * 0.0254
    return {
        "source": str(ses_path),
        "total_length_mm": round(length_mm, 3),
        "via_count": vias,
        "segments": wires,
        "unrouted_nets": [],
        "clearance_violations": 0,
        "notes": ["parsed from SES (approximate length from path vertices)"],
    }


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def compare_metrics(
    topor: dict[str, Any],
    baseline: dict[str, Any] | None = None,
    *,
    label_a: str = "topor",
    label_b: str = "freerouting",
) -> dict[str, Any]:
    """Side-by-side comparison table + relative deltas."""
    out: dict[str, Any] = {
        label_a: topor,
        "winner": {},
        "deltas": {},
    }
    if baseline is None:
        out[label_b] = None
        out["notes"] = [
            f"No {label_b} result provided. Run FreeRouting on the exported DSN "
            "and re-run compare with --ses or --baseline-json."
        ]
        return out

    out[label_b] = baseline
    for key in ("total_length_mm", "via_count", "segments", "clearance_violations"):
        a = float(topor.get(key) or 0)
        b = float(baseline.get(key) or 0)
        out["deltas"][key] = {
            label_a: a,
            label_b: b,
            "delta": round(a - b, 3),
            "pct_vs_baseline": round(100.0 * (a - b) / b, 2) if b else None,
        }
        # lower is better for length/vias/violations
        if a < b:
            out["winner"][key] = label_a
        elif b < a:
            out["winner"][key] = label_b
        else:
            out["winner"][key] = "tie"

    ua = len(topor.get("unrouted_nets") or [])
    ub = len(baseline.get("unrouted_nets") or [])
    out["deltas"]["unrouted_count"] = {label_a: ua, label_b: ub, "delta": ua - ub}
    out["winner"]["unrouted_count"] = (
        label_a if ua < ub else (label_b if ub < ua else "tie")
    )
    return out


def _per_net_table(result: RouteResult) -> dict[str, dict[str, Any]]:
    by_net: dict[str, dict[str, Any]] = {}
    for s in result.segments:
        if not s.net:
            continue
        row = by_net.setdefault(
            s.net,
            {
                "length_mm": 0.0,
                "segments": 0,
                "vias": 0,
                "areas": 0,
                "layers": set(),
                "zone_only": False,
            },
        )
        row["length_mm"] += ((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) ** 0.5
        row["segments"] += 1
        row["layers"].add(s.layer)
    for v in result.vias:
        if not v.net:
            continue
        row = by_net.setdefault(
            v.net,
            {
                "length_mm": 0.0,
                "segments": 0,
                "vias": 0,
                "areas": 0,
                "layers": set(),
                "zone_only": False,
            },
        )
        row["vias"] += 1
    for area in result.areas or []:
        if not area.net:
            continue
        row = by_net.setdefault(
            area.net,
            {
                "length_mm": 0.0,
                "segments": 0,
                "vias": 0,
                "areas": 0,
                "layers": set(),
                "zone_only": False,
            },
        )
        row["areas"] += 1
        row["layers"].add(area.layer)
    out: dict[str, dict[str, Any]] = {}
    for net, row in by_net.items():
        layers = sorted(row["layers"])
        segs = int(row["segments"])
        vias = int(row["vias"])
        areas = int(row["areas"])
        out[net] = {
            "length_mm": round(float(row["length_mm"]), 4),
            "segments": segs,
            "vias": vias,
            "areas": areas,
            "layers": layers,
            "zone_only": areas > 0 and segs == 0 and vias == 0,
            "primary_layer": max(
                layers,
                key=lambda ly: sum(
                    ((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) ** 0.5
                    for s in result.segments
                    if s.net == net and s.layer == ly
                )
                + (1.0 if any(a.net == net and a.layer == ly for a in (result.areas or [])) else 0.0),
            )
            if layers
            else None,
        }
    return out


def compare_to_golden(
    autorouter: RouteResult,
    human: RouteResult,
    *,
    label_ar: str = "autorouter",
    label_human: str = "human",
    hard_violations: int | None = None,
) -> dict[str, Any]:
    """Score an autorouter result against human golden copper.

    Primary signals: completion ratio vs human-copper nets, hard DRC, open nets.
    Secondary: length / via deltas (only meaningful when completion is comparable).
    """
    ar_m = metrics_from_route_result(autorouter, source=label_ar)
    hu_m = metrics_from_route_result(human, source=label_human)
    base = compare_metrics(ar_m, hu_m, label_a=label_ar, label_b=label_human)

    ar_nets = _per_net_table(autorouter)
    hu_nets = _per_net_table(human)
    human_copper_nets = set(hu_nets)
    ar_copper_nets = set(ar_nets)
    human_zone_only = {n for n, r in hu_nets.items() if r.get("zone_only")}
    ar_zone_only = {n for n, r in ar_nets.items() if r.get("zone_only")}

    # Prefer explicit unrouted lists; fall back to copper presence.
    ar_open = set(autorouter.unrouted_nets or [])
    if not ar_open and human_copper_nets:
        ar_open = human_copper_nets - ar_copper_nets
    hu_open = set(human.unrouted_nets or [])

    human_done = sorted(human_copper_nets - hu_open)
    # Zone-only human nets: AR may complete via areas OR tracks/vias.
    ar_done_of_human = sorted(
        n
        for n in human_done
        if n in ar_copper_nets and n not in ar_open
    )
    missing = sorted(n for n in human_done if n not in ar_done_of_human)
    # Pour-only human nets missing AR tracks are soft-missing if AR has no copper:
    # still count as missing for completion, but flag for physics report.
    missing_zone_pours = sorted(n for n in missing if n in human_zone_only)
    extra = sorted(ar_copper_nets - human_copper_nets)

    n_human = max(len(human_done), 1)
    completion_vs_human = len(ar_done_of_human) / n_human

    per_net: list[dict[str, Any]] = []
    for net in sorted(human_copper_nets | ar_copper_nets):
        a = ar_nets.get(net) or {}
        h = hu_nets.get(net) or {}
        al = float(a.get("length_mm") or 0)
        hl = float(h.get("length_mm") or 0)
        av = int(a.get("vias") or 0)
        hv = int(h.get("vias") or 0)
        per_net.append(
            {
                "net": net,
                "ar_length_mm": al,
                "human_length_mm": hl,
                "delta_length_mm": round(al - hl, 4),
                "ar_vias": av,
                "human_vias": hv,
                "delta_vias": av - hv,
                "ar_primary_layer": a.get("primary_layer"),
                "human_primary_layer": h.get("primary_layer"),
                "layer_match": (
                    a.get("primary_layer") == h.get("primary_layer")
                    if a.get("primary_layer") and h.get("primary_layer")
                    else None
                ),
                "ar_complete": net in ar_done_of_human or (
                    net in ar_copper_nets and net not in ar_open
                ),
                "human_has_copper": net in human_copper_nets,
            }
        )

    layer_agree = [
        r for r in per_net if r.get("layer_match") is True and r.get("human_has_copper")
    ]
    layer_denom = [
        r
        for r in per_net
        if r.get("human_has_copper") and r.get("ar_primary_layer") and r.get("human_primary_layer")
    ]

    viol = (
        int(hard_violations)
        if hard_violations is not None
        else int(autorouter.clearance_violations or 0)
    )

    # Policy score: do not reward shorter copper when nets are open.
    policy_ok = viol == 0
    score = 100.0 * completion_vs_human
    if not policy_ok:
        score = min(score, 40.0)
    score -= min(30.0, 5.0 * len(missing))
    if completion_vs_human >= 0.99 and policy_ok:
        # Only then length/via efficiency vs human matters a little
        hl = float(hu_m.get("total_length_mm") or 0) or 1.0
        al = float(ar_m.get("total_length_mm") or 0)
        length_ratio = al / hl
        if length_ratio > 1.25:
            score -= min(15.0, (length_ratio - 1.25) * 40.0)
        hv = float(hu_m.get("via_count") or 0) or 1.0
        av = float(ar_m.get("via_count") or 0)
        if av > hv * 1.5:
            score -= min(10.0, (av / hv - 1.5) * 20.0)
    score = max(0.0, min(100.0, score))

    grade = (
        "A"
        if score >= 90
        else "B"
        if score >= 75
        else "C"
        if score >= 55
        else "D"
        if score >= 35
        else "F"
    )

    out = {
        **base,
        "kind": "golden",
        "completion": {
            "human_nets_with_copper": len(human_done),
            "ar_completed_of_human": len(ar_done_of_human),
            "ratio": round(completion_vs_human, 4),
            "missing_nets": missing,
            "missing_zone_pours": missing_zone_pours,
            "human_zone_only_nets": sorted(human_zone_only),
            "ar_zone_only_nets": sorted(ar_zone_only),
            "extra_ar_nets": extra,
            "ar_unrouted": sorted(ar_open),
            "human_unrouted": sorted(hu_open),
            "human_areas": len(human.areas or []),
            "ar_areas": len(autorouter.areas or []),
        },
        "per_net": per_net,
        "layer_agreement": {
            "matched": len(layer_agree),
            "compared": len(layer_denom),
            "ratio": round(len(layer_agree) / max(len(layer_denom), 1), 4),
        },
        "policy": {
            "hard_violations": viol,
            "zero_hard_drc": policy_ok,
            "prefer_open_over_short": True,
        },
        "golden_score": round(score, 2),
        "golden_grade": grade,
        "notes": [
            "Primary: completion vs human copper + zero hard DRC.",
            "Secondary length/via only counted when AR finishes all human nets.",
        ],
    }
    return out


def write_comparison_markdown(comparison: dict[str, Any], out_path: str | Path) -> Path:
    out_path = Path(out_path)
    if comparison.get("kind") == "golden":
        return write_golden_markdown(comparison, out_path)

    lines = [
        "# Route comparison: TopoR vs FreeRouting baseline",
        "",
        "| Metric | TopoR | FreeRouting | Δ (TopoR − FR) | Winner |",
        "|--------|------:|------------:|---------------:|:------:|",
    ]
    # normalize keys
    a_key = "topor" if "topor" in comparison else [k for k in comparison if k not in ("winner", "deltas", "notes")][0]
    b_key = "freerouting" if "freerouting" in comparison else None
    if b_key is None:
        for k in comparison:
            if k not in (a_key, "winner", "deltas", "notes") and comparison[k]:
                b_key = k
                break

    a = comparison.get(a_key) or {}
    b = comparison.get(b_key) if b_key else None
    deltas = comparison.get("deltas") or {}
    winners = comparison.get("winner") or {}

    if not b:
        lines.append("")
        lines.append("_FreeRouting baseline not available in this run._")
        lines.append("")
        lines.append(f"| total_length_mm | {a.get('total_length_mm', '—')} | — | — | — |")
        lines.append(f"| via_count | {a.get('via_count', '—')} | — | — | — |")
        lines.append(f"| segments | {a.get('segments', '—')} | — | — | — |")
        lines.append(f"| clearance_violations | {a.get('clearance_violations', '—')} | — | — | — |")
        for n in comparison.get("notes") or []:
            lines.append(f"- {n}")
    else:
        for key in ("total_length_mm", "via_count", "segments", "clearance_violations"):
            d = deltas.get(key) or {}
            lines.append(
                f"| {key} | {d.get(a_key, a.get(key))} | {d.get(b_key, b.get(key))} | "
                f"{d.get('delta', '—')} | {winners.get(key, '—')} |"
            )
        ud = deltas.get("unrouted_count") or {}
        lines.append(
            f"| unrouted_count | {ud.get(a_key, 0)} | {ud.get(b_key, 0)} | "
            f"{ud.get('delta', 0)} | {winners.get('unrouted_count', '—')} |"
        )

    lines.append("")
    lines.append("## Sources")
    lines.append(f"- TopoR: `{a.get('source', '')}`")
    if b:
        lines.append(f"- FreeRouting: `{b.get('source', '')}`")
    lines.append("")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def write_golden_markdown(comparison: dict[str, Any], out_path: str | Path) -> Path:
    """Markdown report for autorouter vs human golden copper."""
    out_path = Path(out_path)
    c = comparison.get("completion") or {}
    pol = comparison.get("policy") or {}
    skip = {
        "winner",
        "deltas",
        "notes",
        "kind",
        "completion",
        "per_net",
        "layer_agreement",
        "policy",
        "golden_score",
        "golden_grade",
    }
    ar = comparison.get("autorouter")
    hu = comparison.get("human")
    if not isinstance(ar, dict) or "total_length_mm" not in ar:
        for k, v in comparison.items():
            if k in skip or not isinstance(v, dict):
                continue
            if "total_length_mm" in v and k != "human":
                ar = v
                break
    if not isinstance(hu, dict) or "total_length_mm" not in hu:
        hu = comparison.get("human")
        if not isinstance(hu, dict) or "total_length_mm" not in hu:
            for k, v in comparison.items():
                if k in skip or not isinstance(v, dict):
                    continue
                if v is ar:
                    continue
                if "total_length_mm" in v:
                    hu = v
                    break
    ar = ar if isinstance(ar, dict) else {}
    hu = hu if isinstance(hu, dict) else {}

    deltas = comparison.get("deltas") or {}
    lines = [
        "# Golden route comparison (autorouter vs human)",
        "",
        f"**Grade:** {comparison.get('golden_grade', '—')} "
        f"({comparison.get('golden_score', '—')}/100)",
        "",
        "## Completion",
        "",
        f"- Human nets with copper: **{c.get('human_nets_with_copper', '—')}**",
        f"- AR completed of those: **{c.get('ar_completed_of_human', '—')}** "
        f"(ratio {c.get('ratio', '—')})",
        f"- Hard DRC violations: **{pol.get('hard_violations', '—')}** "
        f"(zero_hard_drc={pol.get('zero_hard_drc')})",
        f"- Missing nets: `{', '.join(c.get('missing_nets') or []) or '—'}`",
        "",
        "## Aggregate (AR vs human)",
        "",
        "| Metric | Autorouter | Human | Δ |",
        "|--------|----------:|------:|--:|",
    ]
    for key in ("total_length_mm", "via_count", "segments"):
        d = deltas.get(key) or {}
        av = d.get("autorouter", ar.get(key, "—"))
        hv = d.get("human", hu.get(key, "—"))
        delta = d.get("delta", "—")
        lines.append(f"| {key} | {av} | {hv} | {delta} |")

    la = comparison.get("layer_agreement") or {}
    lines.extend(
        [
            "",
            f"Layer primary agreement: {la.get('matched', 0)}/{la.get('compared', 0)} "
            f"(ratio {la.get('ratio', 0)})",
            "",
            "## Per-net (first 30)",
            "",
            "| Net | AR L (mm) | Human L | ΔL | AR vias | Human vias | Layer match |",
            "|-----|----------:|--------:|---:|--------:|-----------:|:-----------:|",
        ]
    )
    for row in (comparison.get("per_net") or [])[:30]:
        lines.append(
            f"| {row.get('net')} | {row.get('ar_length_mm')} | {row.get('human_length_mm')} | "
            f"{row.get('delta_length_mm')} | {row.get('ar_vias')} | {row.get('human_vias')} | "
            f"{row.get('layer_match')} |"
        )
    lines.append("")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
