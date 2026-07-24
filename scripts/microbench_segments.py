#!/usr/bin/env python3
"""Fast segment microbenches for mppc score levers (seconds–minutes, not hours).

Usage::

    PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment 2pin
    PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment analog
    PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment hspeed
    PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment all --timeout 180
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "native" / "build")]

PCB = ROOT / "examples/mppc-interface/mppcInterface_v1.3.kicad_pcb"
CFG = ROOT / "examples/mppc-interface/placement_config.yaml"
OUT = ROOT / "viewer/runs/microbench"


def _select_nets(board, segment: str) -> list[str]:
    names = list(board.nets.keys())

    def pins(n: str) -> int:
        return len(board.nets.get(n) or [])

    if segment == "2pin":
        return sorted([n for n in names if pins(n) <= 2])
    if segment == "analog":
        return sorted(
            [
                n
                for n in names
                if n.upper().startswith(("CH", "DAC", "ADC")) and pins(n) >= 2
            ]
        )
    if segment == "hspeed":
        keys = ("CLK", "SCLK", "MOSI", "MISO", "CS", "SPI", "USB", "XTAL")
        return sorted(
            [n for n in names if any(k in n.upper() for k in keys)]
        )
    if segment == "local_rc":
        return sorted(
            [
                n
                for n in names
                if n.startswith("Net-(") and pins(n) <= 2
            ]
        )
    if segment == "power":
        keys = ("GND", "VCC", "VDD", "+3V", "+5V", "HV", "VSS", "PGND")
        return sorted(
            [n for n in names if any(k in n.upper() for k in keys)]
        )
    raise SystemExit(f"unknown segment: {segment}")


def main() -> int:
    # Line-buffer stdout so long routes show progress under tee/pipes
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--segment",
        choices=["2pin", "analog", "hspeed", "local_rc", "power", "all"],
        default="2pin",
    )
    ap.add_argument("--grid", type=float, default=0.15)
    ap.add_argument("--fine-grid", type=float, default=0.10, help="retry grid for failures")
    ap.add_argument("--timeout", type=float, default=300.0, help="soft wall seconds")
    ap.add_argument("--pcb", type=Path, default=PCB)
    ap.add_argument("--config", type=Path, default=CFG)
    args = ap.parse_args()

    if not args.pcb.is_file():
        print(f"missing PCB: {args.pcb}", file=sys.stderr)
        return 2

    from physics_router.config_io import load_config
    from physics_router.design_rules import load_design_rules
    from physics_router.kicad_io import load_board_from_kicad_pcb
    from physics_router.router import (
        _net_fully_connected,
        clearance_aware_route,
    )

    cfg = load_config(args.config) if args.config.is_file() else None
    board = load_board_from_kicad_pcb(args.pcb, cfg)
    rules = load_design_rules(pcb_path=args.pcb)
    cl = float(rules.constraints.min_clearance_mm or 0.2)

    segments = (
        ["2pin", "local_rc", "analog", "hspeed"]
        if args.segment == "all"
        else [args.segment]
    )

    OUT.mkdir(parents=True, exist_ok=True)
    t_global = time.time()
    rows = []

    for seg in segments:
        if time.time() - t_global > args.timeout:
            print(f"soft timeout {args.timeout}s — stop before {seg}")
            break
        nets = _select_nets(board, seg)
        print(f"\n=== segment={seg} nets={len(nets)} grid={args.grid} ===")
        if not nets:
            print("  (none)")
            continue

        t0 = time.time()
        r = clearance_aware_route(
            board,
            cfg,
            clearance_mm=cl,
            grid_mm=float(args.grid),
            soft_fallback=False,
            prefer_native=True,
            allow_vias=True,
            nets_filter=nets,
            net_order=nets,
            design_rules=rules,
            skip_hybrid=True,
        )
        ok = [
            n
            for n in nets
            if _net_fully_connected(
                board, n, r.segments, r.vias, areas=r.areas
            )
        ]
        fail = [n for n in nets if n not in ok]
        dt = time.time() - t0
        print(f"  pass1: {len(ok)}/{len(nets)} in {dt:.1f}s")

        # Fine residual only on failures (lever 1 / 6)
        if fail:
            t1 = time.time()
            r2 = clearance_aware_route(
                board,
                cfg,
                clearance_mm=cl,
                grid_mm=float(args.fine_grid),
                soft_fallback=False,
                prefer_native=True,
                allow_vias=True,
                nets_filter=fail,
                net_order=fail,
                seed_result=r,
                design_rules=rules,
                skip_hybrid=True,
            )
            # merge successful fine nets into view
            ok2 = []
            for n in fail:
                segs = [s for s in r2.segments if s.net == n]
                vias = [v for v in r2.vias if v.net == n]
                areas = [a for a in r2.areas if a.net == n]
                if segs or areas:
                    if _net_fully_connected(board, n, segs, vias, areas=areas):
                        ok2.append(n)
                        r.segments.extend(segs)
                        r.vias.extend(vias)
                        r.areas.extend(areas)
            still = [n for n in fail if n not in ok2]
            dt2 = time.time() - t1
            print(
                f"  fine@{args.fine_grid}: recovered {len(ok2)}/{len(fail)} "
                f"in {dt2:.1f}s · still open {len(still)}"
            )
            if still:
                print(f"  open: {', '.join(still[:24])}")
            ok = ok + ok2
            fail = still
        else:
            print("  fine: skipped (all complete)")

        row = {
            "segment": seg,
            "n_nets": len(nets),
            "ok": len(ok),
            "fail": len(fail),
            "completion": round(len(ok) / max(1, len(nets)), 4),
            "open": fail,
            "time_s": round(time.time() - t0, 2),
            "grid": args.grid,
            "fine_grid": args.fine_grid,
        }
        rows.append(row)
        print(
            f"  RESULT {seg}: {len(ok)}/{len(nets)} "
            f"({100 * row['completion']:.1f}%) in {row['time_s']}s"
        )

    out_path = OUT / f"segments_{int(time.time())}.json"
    out_path.write_text(json.dumps({"rows": rows}, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out_path}")
    # non-zero if any segment < 100% on 2pin/local_rc (the "easy 80%")
    easy_bad = [
        r
        for r in rows
        if r["segment"] in ("2pin", "local_rc") and r["fail"] > 0
    ]
    return 1 if easy_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
