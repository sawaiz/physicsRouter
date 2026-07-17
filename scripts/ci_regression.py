#!/usr/bin/env python3
"""CI regression: score synthetic (+ HALO-90 if present) and fail on large regressions.

  python scripts/ci_regression.py
  python scripts/ci_regression.py --update-baselines
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from physics_router.config_io import example_config, load_config
from physics_router.kicad_io import board_from_synthetic, load_board_from_kicad_pcb
from physics_router.physics import geometric_score
from physics_router.router import topological_guide_route

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "ci" / "baselines" / "scores.json"
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
HALO_CFG = ROOT / "examples/halo-90/placement_config.yaml"

# Relative tolerance for score_total / length (placement scores can be large)
TOL_SCORE = 0.25  # 25% — placement SA is stochastic; score on fixed place is stable
TOL_LENGTH = 0.15


def measure_synthetic() -> dict:
    cfg = example_config()
    board = board_from_synthetic(cfg)
    sb = geometric_score(board, cfg)
    guide = topological_guide_route(board, cfg)
    return {
        "score_total": sb.total,
        "guide_length_mm": guide.total_length_mm,
        "components": len(board.components),
        "nets": len(board.nets),
    }


def measure_halo() -> dict | None:
    if not HALO_PCB.exists() or not HALO_CFG.exists():
        return None
    cfg = load_config(HALO_CFG)
    board = load_board_from_kicad_pcb(HALO_PCB, cfg)
    sb = geometric_score(board, cfg)
    guide = topological_guide_route(board, cfg)
    return {
        "score_total": sb.total,
        "guide_length_mm": guide.total_length_mm,
        "components": len(board.components),
        "nets": len(board.nets),
    }


def check(name: str, measured: dict, baseline: dict) -> list[str]:
    fails = []
    for key, tol in (("score_total", TOL_SCORE), ("guide_length_mm", TOL_LENGTH)):
        if key not in baseline or key not in measured:
            continue
        b, m = float(baseline[key]), float(measured[key])
        if b == 0:
            continue
        # Fail if score/length got much *worse* (higher)
        if m > b * (1.0 + tol):
            fails.append(
                f"{name}.{key} regression: measured {m:.3f} > baseline {b:.3f} (+{tol*100:.0f}% allowed)"
            )
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-baselines", action="store_true")
    args = ap.parse_args()

    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    measured = {"synthetic": measure_synthetic()}
    halo = measure_halo()
    if halo:
        measured["halo-90"] = halo

    if args.update_baselines or not BASELINE.exists():
        # preserve extra keys
        old = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
        old.update(measured)
        BASELINE.write_text(json.dumps(old, indent=2) + "\n", encoding="utf-8")
        print("Updated baselines:", BASELINE)
        print(json.dumps(measured, indent=2))
        return 0

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    fails: list[str] = []
    for name, m in measured.items():
        b = baseline.get(name)
        if not b:
            print(f"WARN: no baseline for {name}, skipping")
            continue
        fails.extend(check(name, m, b))

    print(json.dumps({"measured": measured, "fails": fails}, indent=2))
    if fails:
        print("REGRESSION FAILURES:", file=sys.stderr)
        for f in fails:
            print(" -", f, file=sys.stderr)
        return 1
    print("OK — no regressions beyond tolerance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
