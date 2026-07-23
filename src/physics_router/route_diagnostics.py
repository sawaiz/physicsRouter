"""Structured routing diagnostics for post-mortem and grade improvement.

Produces machine-readable JSON + human markdown so every golden / capacity
run leaves a trail: *what* failed, *which class* of nets, *why* (heuristics),
and *what to try next*.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from physics_router.router import RouteResult


# ---------------------------------------------------------------------------
# Heuristic failure categories (actionable for grade improvement)
# ---------------------------------------------------------------------------

_POWER_RE = re.compile(
    r"^(GND|AGND|DGND|PGND|VCC|VDD|VSS|VBAT|HV|\+|\-)", re.I
)
_CHANNEL_RE = re.compile(r"^(CH|DAC|ADC|IN|OUT)\d*", re.I)
_SPI_RE = re.compile(
    r"(CLK|SCLK|MISO|MOSI|CS|SDA|SCL|SPI|I2C|UART|TX|RX)", re.I
)
_GPIO_RE = re.compile(r"^(GPIO|IO|GP)\d*", re.I)
_LED_RE = re.compile(r"^LED", re.I)
_LOCAL_NET_RE = re.compile(r"^Net-\(", re.I)


def _net_category(net: str) -> str:
    if _POWER_RE.search(net) or net.upper() in {"GND", "AGND", "DGND"}:
        return "power_gnd"
    if _CHANNEL_RE.search(net):
        return "analog_channel"
    if _SPI_RE.search(net):
        return "digital_bus"
    if _GPIO_RE.search(net):
        return "gpio"
    if _LED_RE.search(net):
        return "led"
    if _LOCAL_NET_RE.search(net):
        return "local_rc"
    return "other"


def _parse_ripup_notes(notes: list[str]) -> list[dict[str, Any]]:
    """Extract rip-up attempts from router note strings."""
    events: list[dict[str, Any]] = []
    # ripup(empty): FPGA_{DONE} vs Net-(C21-Pad1),Net-(L5-Pad1) (attempt 1)
    pat = re.compile(
        r"ripup\((\w+)\):\s*(\S+)\s+vs\s+(.+?)\s*\(attempt\s+(\d+)\)",
        re.I,
    )
    for note in notes or []:
        m = pat.search(str(note))
        if not m:
            continue
        peers = [p.strip() for p in m.group(3).split(",") if p.strip()]
        events.append(
            {
                "outcome": m.group(1).lower(),
                "net": m.group(2),
                "peers": peers,
                "attempt": int(m.group(4)),
            }
        )
    return events


def _parse_phase_notes(notes: list[str]) -> list[dict[str, Any]]:
    phases: list[dict[str, Any]] = []
    # hybrid phase general: +318 segs +94 vias +3 areas unrouted=36
    pat = re.compile(
        r"hybrid phase (\w+):\s*\+(\d+) segs \+(\d+) vias \+(\d+) areas unrouted=(\d+)",
        re.I,
    )
    for note in notes or []:
        m = pat.search(str(note))
        if m:
            phases.append(
                {
                    "strategy": m.group(1),
                    "segments": int(m.group(2)),
                    "vias": int(m.group(3)),
                    "areas": int(m.group(4)),
                    "unrouted": int(m.group(5)),
                }
            )
    return phases


def _pin_count(board: Any, net: str) -> int:
    if board is None:
        return 0
    nets = getattr(board, "nets", None) or {}
    return len(nets.get(net) or [])


def analyze_route_result(
    ar: RouteResult,
    *,
    human: RouteResult | None = None,
    board: Any = None,
    comparison: dict[str, Any] | None = None,
    board_id: str = "board",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured difficulty / failure report from a route result."""
    q = ar.quality or {}
    notes = list(ar.notes or [])
    unrouted = list(ar.unrouted_nets or [])
    reports = {r.net: r for r in (ar.net_reports or []) if r.net}
    ok_nets = sorted(n for n, r in reports.items() if r.status == "ok")
    status_counts = Counter(r.status for r in (ar.net_reports or []))

    missing = list((comparison or {}).get("completion", {}).get("missing_nets") or unrouted)
    if not missing:
        missing = unrouted

    # Categories of missing nets
    by_cat: dict[str, list[str]] = defaultdict(list)
    for n in missing:
        by_cat[_net_category(n)].append(n)

    pin_stats = []
    for n in missing:
        pin_stats.append({"net": n, "pins": _pin_count(board, n), "category": _net_category(n)})
    pin_stats.sort(key=lambda x: (-x["pins"], x["net"]))

    # Bloat: AR much longer than human on completed nets (corridor hogging)
    bloat: list[dict[str, Any]] = []
    if human is not None and comparison:
        for row in comparison.get("per_net") or []:
            if not row.get("ar_complete"):
                continue
            hl = float(row.get("human_length_mm") or 0)
            al = float(row.get("ar_length_mm") or 0)
            if hl > 1.0 and al > hl * 1.75:
                bloat.append(
                    {
                        "net": row.get("net"),
                        "ar_mm": round(al, 2),
                        "human_mm": round(hl, 2),
                        "ratio": round(al / hl, 2),
                        "ar_vias": row.get("ar_vias"),
                        "human_vias": row.get("human_vias"),
                    }
                )
        bloat.sort(key=lambda x: -float(x.get("ratio") or 0))

    ripups = _parse_ripup_notes(notes)
    ripup_by_net = Counter(e["net"] for e in ripups)
    empty_ripups = [e for e in ripups if e.get("outcome") == "empty"]
    phases = _parse_phase_notes(notes)

    gate = q.get("manufacturing_gate") or {}
    hybrid = q.get("hybrid_plan") or {}
    plan_metrics = (q.get("production_route_plan") or {}).get("metrics") or {}
    shared = q.get("shared_escape") or plan_metrics.get("shared_escape") or {}
    via_prof = q.get("via_profile") or {}

    # Difficulty signals
    difficulties: list[dict[str, Any]] = []

    n_miss = len(missing)
    n_total = max(len(ok_nets) + n_miss, 1)
    completion = len(ok_nets) / n_total if not comparison else float(
        (comparison.get("completion") or {}).get("ratio") or (len(ok_nets) / n_total)
    )

    if n_miss:
        difficulties.append(
            {
                "id": "incomplete_nets",
                "severity": "high" if completion < 0.7 else "medium",
                "summary": f"{n_miss} nets open ({completion:.0%} complete)",
                "detail": {
                    "missing_count": n_miss,
                    "by_category": {k: len(v) for k, v in sorted(by_cat.items())},
                },
                "improvements": [
                    "Raise net weights for missing power/bus nets in placement_config.yaml",
                    "Route power with pours/zones after legal tracks (improve --physics-feedback)",
                    "Prefer few-pin nets first within power class so GND does not starve last",
                ],
            }
        )

    power_miss = by_cat.get("power_gnd") or []
    if power_miss:
        difficulties.append(
            {
                "id": "power_gnd_open",
                "severity": "high",
                "summary": f"Power/GND open: {', '.join(power_miss[:8])}",
                "detail": {
                    "nets": power_miss,
                    "ar_areas": len(ar.areas or []),
                    "human_areas": len(human.areas or []) if human else None,
                },
                "improvements": [
                    "Human goldens use large pours (zones) for GND/power — AR under-uses areas",
                    "Add dedicated power-plane / zone fill stage after multipin connectivity",
                    "Ensure config lists +5V/+3V3/GND with net_class power/ground and high weight",
                ],
            }
        )

    if empty_ripups:
        top = ripup_by_net.most_common(8)
        difficulties.append(
            {
                "id": "ripup_exhausted",
                "severity": "high",
                "summary": f"{len(empty_ripups)} empty rip-up attempts (no legal alternate)",
                "detail": {
                    "events": len(empty_ripups),
                    "top_nets": [{"net": n, "attempts": c} for n, c in top],
                    "sample": empty_ripups[:12],
                },
                "improvements": [
                    "PathFinder-style negotiated congestion on conflict clusters (enable for <70 nets)",
                    "Increase rip-up budget and peer set for multipin nets",
                    "Re-order: route short 2-pin locals before long buses that seal corridors",
                ],
            }
        )

    if bloat:
        difficulties.append(
            {
                "id": "corridor_hogging",
                "severity": "medium",
                "summary": f"{len(bloat)} completed nets >> human length (corridor hogging)",
                "detail": {"worst": bloat[:8]},
                "improvements": [
                    "Cap detour ratio vs Steiner/MST guide length during commit",
                    "Charge shared capacity earlier so early nets cannot monopolize lanes",
                    "Elastic regeometry / rubber-band after full legal set",
                ],
            }
        )

    overflow = plan_metrics.get("final_overflow")
    mesh_ov = plan_metrics.get("mesh_overflow_nodes")
    if overflow is not None and int(overflow) > 0:
        difficulties.append(
            {
                "id": "global_overflow",
                "severity": "medium",
                "summary": f"Global section overflow={overflow}, mesh_overflow_nodes={mesh_ov}",
                "detail": {
                    "final_overflow": overflow,
                    "overflow_history": plan_metrics.get("overflow_history"),
                    "mesh_overflow_nodes": mesh_ov,
                    "planned_vias": plan_metrics.get("planned_vias"),
                },
                "improvements": [
                    "Raise capacity_effort / mesh depth on dense boards",
                    "Overflow-aware Steiner already on — feed occupancy into detail cost more strongly",
                    "Split multipin nets into hierarchical sections (CBS / tree packing)",
                ],
            }
        )

    if hybrid.get("counts"):
        counts = hybrid["counts"]
        if int(counts.get("matrix") or 0) == 0 and n_miss > 10:
            difficulties.append(
                {
                    "id": "no_matrix_bucket",
                    "severity": "low",
                    "summary": f"No matrix strategy nets (counts={counts}); dense multipin treated as general",
                    "detail": {"counts": counts},
                    "improvements": [
                        "Classify multipin analog channels (CH*, DAC*) as matrix when pin count ≥ 3",
                        "Use finer grid (0.15) for channel fanout like matrix phase",
                    ],
                }
            )

    hard = int(
        (comparison or {}).get("policy", {}).get("hard_violations")
        if comparison
        else (ar.clearance_violations or 0)
    )
    if hard == 0 and n_miss:
        difficulties.append(
            {
                "id": "open_over_short_ok",
                "severity": "info",
                "summary": "Hard DRC = 0 with open nets (policy working)",
                "detail": {"hard_violations": 0, "completion": round(completion, 4)},
                "improvements": [
                    "Keep manufacturing gate: never trade shorts for completion",
                    "Grade is dominated by completion ratio — fix open nets, not length",
                ],
            }
        )

    # Grade math transparency
    grade_info = {
        "golden_grade": (comparison or {}).get("golden_grade") or q.get("grade"),
        "golden_score": (comparison or {}).get("golden_score") or q.get("score"),
        "completion_ratio": round(completion, 4),
        "score_formula": (
            "score ≈ 100*completion − min(30, 5*missing_nets); "
            "length/via bonuses only when completion ≥ 99%"
        ),
        "to_reach_D_35": "Need roughly completion ≥ 0.65 with 0 hard DRC",
        "to_reach_C_55": "Need roughly completion ≥ 0.85 with 0 hard DRC",
        "to_reach_B_75": "Need roughly completion ≥ 0.95 with 0 hard DRC",
        "to_reach_A_90": "Need roughly completion ≥ 0.99 + efficiency",
    }

    # Prioritized next actions
    actions: list[str] = []
    for d in difficulties:
        if d["severity"] in ("high", "medium"):
            actions.extend(d.get("improvements") or [])
    # unique preserve order
    seen: set[str] = set()
    actions_u = []
    for a in actions:
        if a not in seen:
            seen.add(a)
            actions_u.append(a)

    report: dict[str, Any] = {
        "kind": "route_diagnostics",
        "board_id": board_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": {
            "ok_nets": len(ok_nets),
            "unrouted_nets": n_miss,
            "completion": round(completion, 4),
            "segments": len(ar.segments),
            "vias": ar.via_count,
            "areas": len(ar.areas or []),
            "length_mm": round(float(ar.total_length_mm or 0), 3),
            "hard_drc": hard,
            "manufacturing_gate_passed": bool(gate.get("passed")) if gate else None,
            "pipeline": q.get("pipeline"),
            "status_counts": dict(status_counts),
        },
        "grade": grade_info,
        "missing_by_category": {k: sorted(v) for k, v in sorted(by_cat.items())},
        "missing_pin_stats": pin_stats[:40],
        "hybrid_plan": {
            "counts": hybrid.get("counts"),
            "notes": hybrid.get("notes"),
        },
        "phases": phases,
        "ripup": {
            "total_events": len(ripups),
            "empty_events": len(empty_ripups),
            "top_nets": [{"net": n, "attempts": c} for n, c in ripup_by_net.most_common(12)],
            "sample": empty_ripups[:20],
        },
        "corridor_bloat": bloat[:15],
        "capacity": {
            "final_overflow": plan_metrics.get("final_overflow"),
            "overflow_history": plan_metrics.get("overflow_history"),
            "mesh_overflow_nodes": mesh_ov,
            "planned_vias": plan_metrics.get("planned_vias"),
            "layer_sections": plan_metrics.get("layer_sections"),
            "shared_escape": shared,
            "via_profile": via_prof,
        },
        "difficulties": difficulties,
        "recommended_actions": actions_u[:12],
        "router_notes_tail": notes[-30:],
        "extra": extra or {},
    }
    return report


def write_diagnostics(
    report: dict[str, Any],
    out_dir: str | Path,
    *,
    basename: str = "route_diagnostics",
) -> dict[str, str]:
    """Write JSON + markdown diagnostics under ``out_dir``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{basename}.json"
    md_path = out_dir / f"{basename}.md"
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    md_path.write_text(diagnostics_to_markdown(report), encoding="utf-8")
    return {"json": str(json_path), "md": str(md_path)}


def diagnostics_to_markdown(report: dict[str, Any]) -> str:
    s = report.get("summary") or {}
    g = report.get("grade") or {}
    lines = [
        f"# Route diagnostics — {report.get('board_id', 'board')}",
        "",
        f"_Generated {report.get('generated_at')}_",
        "",
        "## Summary",
        "",
        f"- Completion: **{s.get('completion')}** "
        f"({s.get('ok_nets')} ok · {s.get('unrouted_nets')} open)",
        f"- Grade / score: **{g.get('golden_grade')}** / {g.get('golden_score')}",
        f"- Hard DRC: **{s.get('hard_drc')}** · gate passed: {s.get('manufacturing_gate_passed')}",
        f"- Copper: {s.get('segments')} segs · {s.get('vias')} vias · "
        f"{s.get('areas')} areas · {s.get('length_mm')} mm",
        f"- Pipeline: `{s.get('pipeline')}`",
        "",
        f"**Score formula:** {g.get('score_formula')}",
        "",
        f"- Toward D (≥35): {g.get('to_reach_D_35')}",
        f"- Toward C (≥55): {g.get('to_reach_C_55')}",
        f"- Toward B (≥75): {g.get('to_reach_B_75')}",
        f"- Toward A (≥90): {g.get('to_reach_A_90')}",
        "",
        "## Missing nets by category",
        "",
    ]
    for cat, nets in (report.get("missing_by_category") or {}).items():
        lines.append(f"- **{cat}** ({len(nets)}): `{', '.join(nets[:20])}`")
    lines += ["", "## Difficulties", ""]
    for d in report.get("difficulties") or []:
        lines.append(f"### [{d.get('severity')}] {d.get('id')}")
        lines.append("")
        lines.append(d.get("summary") or "")
        lines.append("")
        for imp in d.get("improvements") or []:
            lines.append(f"- {imp}")
        lines.append("")
    lines += ["## Recommended actions", ""]
    for i, a in enumerate(report.get("recommended_actions") or [], 1):
        lines.append(f"{i}. {a}")
    lines += ["", "## Hybrid phases", ""]
    for p in report.get("phases") or []:
        lines.append(
            f"- `{p.get('strategy')}`: +{p.get('segments')} segs "
            f"+{p.get('vias')} vias unrouted={p.get('unrouted')}"
        )
    rip = report.get("ripup") or {}
    lines += [
        "",
        "## Rip-up",
        "",
        f"- Empty attempts: **{rip.get('empty_events')}** / {rip.get('total_events')}",
        "",
    ]
    for t in rip.get("top_nets") or []:
        lines.append(f"- `{t.get('net')}`: {t.get('attempts')} attempts")
    bloat = report.get("corridor_bloat") or []
    if bloat:
        lines += ["", "## Corridor bloat (AR ≫ human length)", "", "| Net | AR mm | Human mm | Ratio |", "|-----|------:|---------:|------:|"]
        for b in bloat[:10]:
            lines.append(
                f"| {b.get('net')} | {b.get('ar_mm')} | {b.get('human_mm')} | {b.get('ratio')} |"
            )
    cap = report.get("capacity") or {}
    lines += [
        "",
        "## Capacity / pin-access",
        "",
        f"- final_overflow: {cap.get('final_overflow')}",
        f"- mesh_overflow_nodes: {cap.get('mesh_overflow_nodes')}",
        f"- planned_vias: {cap.get('planned_vias')}",
        f"- via_profile: {(cap.get('via_profile') or {}).get('selected')}",
        f"- shared_escape savings_ratio: "
        f"{(cap.get('shared_escape') or {}).get('savings_ratio') or (cap.get('shared_escape') or {}).get('savings_ratio_sites')}",
        "",
        "## Router notes (tail)",
        "",
        "```",
    ]
    for n in report.get("router_notes_tail") or []:
        lines.append(str(n)[:200])
    lines += ["```", ""]
    return "\n".join(lines) + "\n"


class StageTimer:
    """Lightweight stage progress logger for capacity / golden runs."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.events: list[dict[str, Any]] = []
        self._t0 = time.time()
        self._last = self._t0

    def mark(
        self,
        stage: str,
        *,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        ev = {
            "t_s": round(now - self._t0, 3),
            "dt_s": round(now - self._last, 3),
            "stage": stage,
            "status": status,
            "detail": detail or {},
        }
        self._last = now
        self.events.append(ev)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "started_s_ago": round(now - self._t0, 3),
                        "events": self.events,
                    },
                    indent=2,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_s": round(time.time() - self._t0, 3),
            "events": list(self.events),
        }
