# Score roadmap — mppc-first, points-per-effort

**TL;DR:** Optimize **completion with 0 hard DRC**. Current desktop golden
(Windows RTX 3070, capacity effort 0.55): **D / 39.41 · 59/85 (69.4%) · 0 DRC**.
Each recovered net is worth ~**+1.2** score until missing &lt; 6, then ~**+6**.

```text
score ≈ 100 × completion − min(30, 5 × missing_nets)
length / via bonuses only when completion ≥ 99%
```

| Target | Need (approx) | Notes |
|--------|---------------|--------|
| C (≥55) | ~85% complete | ~72/85 nets |
| B (≥75) | ~95% complete | ~81/85 |
| A (≥90) | ≥99% + length/via economy + clean KiCad DRC | after pours |

Latest stage evidence (Windows `mppc_v1.3_win`):

```text
80_20 routine_2pin: parallel 10/20 + serial residual 1/10  → ~11/20
80_20 mid_3to6:     46/59
80_20 heavy_multipin: 1/6
completion_recovery: +1 two-pin salvage; multipin restore 8/8
open: 26 (power/GND, CH/DAC, SPI, GPIO, 2 local_rc)
```

**Test policy:** full mppc capacity runs are ~1–2 h. Prefer **segment microbenches**
(2-pin only, analog CH/DAC only, digital bus only) under a few minutes before
re-running the full golden.

---

## Six levers (priority order)

### 1. Fix routine 2-pin leak (~+9 nets) — **bug, not capacity**

Stage 1 routes on a nearly empty board and only completes ~11/20, while mid
3–6-pin hits ~78%. Failures are dense `local_rc` clusters (`Net-(R*-Pad2)`,
`Net-(C*-Pad1)`): pin-escape / grid, not congestion.

**Do:**

- Per-failure pin-access logging (which pad, nearest obstacle).
- Residual 2-pin pass at **0.10 mm** grid + higher native expansions.
- Escape-via fallback when same-layer pad-to-pad is blocked.
- Unit / microbench: mppc 2-pin-only + synthetic dense RC pair.

**Done when:** routine_2pin ≥ 19/20 on empty board; full golden local_rc ≤ 1 open.

### 2. Pour synthesis for power/GND (~+5 nets + corridor relief)

Human: **61** zones; AR: **2**. GND is 521 mm human copper — tracks alone lose.
Native already has `CopperArea` / organic outline; golden tracks `missing_zone_pours`.

**Do:** after legal signal copper: minimal power spine → grow pours → KiCad
zone refill as authority → gate + golden treat zone-connected pads as complete.

### 3. Conflict-directed rip-up (fix empty rip-ups)

`_space_rip_candidates` sorts by priority / pin count / name — not nets that
*block the failed search*. Log showed GPIO17 ripping alphabetical Cap nets.

**Do:** soft-cost foreign copper → min-conflict path → rip only crossed nets
(PathFinder-style detail negotiation).

### 4. Matrix bucket for analog channels (~+10–14 with #3)

`CH0–7` / `DAC*` are parallel fanouts mis-served as general. Classification
already labels multipin CH/DAC as matrix; ensure 80/20 / hybrid **actually
uses** matrix grid (0.15) and shared corridor plan for those nets.

**Microbench:** route only `CH*`+`DAC*` with matrix policy (seconds–minutes).

### 5. Detour-ratio commit cap

Reject commit when length &gt; α × Steiner guide (α ≈ 1.6–2) → renegotiate
instead of corridor hogging (`+5V-A` 302 vs 85 mm human).

### 6. Coarse-to-fine wall time

~most minutes in fine-grid `segment_blocked`. First pass 0.2 mm; re-route
**failures only** at 0.1 mm; spend saved time on rip-up/negotiation.

---

## Projected trajectory

| After | Completion (est.) | Grade band |
|-------|------------------:|:----------:|
| #1 | ~80% | C boundary |
| +#2 | ~86% | C |
| +#3/#4 | ~95% | B |
| +pours + KiCad clean + economy | ≥99% | A possible |

---

## Segment microbench commands

```bash
# 2-pin only (lever 1)
PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment 2pin

# Local RC subset (should be ~100% on empty board)
PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment local_rc

# Analog channels / DACs (lever 4)
PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment analog

# High-speed / digital bus (SPI, CLK)
PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment hspeed

# Full golden (slow — only after segment green)
python scripts/run_mppc_benchmark.py
```

### Measured (Mac M3, empty board, 2026-07-24)

| Segment | Result | Wall | Notes |
|---------|--------|-----:|-------|
| `local_rc` | **6/6 (100%)** | 0.5 s | No residual needed |
| `2pin` sequential | 12/20 → fine 0.10 → **15/20** | ~5 min | Open: GPIO17/18/23, LED-0, `~FPGA_{RST}` |
| `2pin` via 80/20 | parallel 9 + residual 0.10 **+7** → **16/20** | **15 s** | Fine 0.08: +0; open GPIO/LED/FPGA done |
| Full golden (Win) | 59/85 D 39.41 | ~98 min | Prior baseline |

**Insight:** true `local_rc` is not the remaining 2-pin leak after fine residual —
longer 2-pin **GPIO / LED / FPGA** nets still fail escape/corridor even on empty
board. Next on lever 1: directed escape-via + layer preference for those, not
more grid refinement.

See also: [ROUTING_DIFFICULTIES.md](ROUTING_DIFFICULTIES.md) ·
[DEEPPCB_NOTES.md](DEEPPCB_NOTES.md) · [MPPC_BENCHMARK.md](MPPC_BENCHMARK.md).
