"""Compare TopoR route results vs FreeRouting / alternate JSON routes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_route_metrics(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "source": str(path),
        "total_length_mm": float(data.get("total_length_mm") or 0),
        "via_count": int(data.get("via_count") or 0),
        "segments": len(data.get("segments") or []),
        "unrouted_nets": list(data.get("unrouted_nets") or []),
        "clearance_violations": int(data.get("clearance_violations") or 0),
        "notes": list(data.get("notes") or [])[:12],
    }


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


def write_comparison_markdown(comparison: dict[str, Any], out_path: str | Path) -> Path:
    out_path = Path(out_path)
    lines = [
        "# Route comparison: TopoR vs FreeRouting baseline",
        "",
        "| Metric | TopoR | FreeRouting | Δ (TopoR − FR) | Winner |",
        "|--------|------:|------------:|---------------:|:------:|",
    ]
    topor = comparison.get("topor") or comparison.get(list(comparison.keys())[0])
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
