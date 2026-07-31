# Score roadmap — mppc-first, points-per-effort

**TL;DR:** Optimize **completion with 0 hard DRC**. Flagship desktop golden
(Windows RTX 3070, capacity effort 0.55, ~98 min): **D / 39.41 · 59/85 (69.4%) · 0 DRC**.
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

---

## Latest full golden (Windows `mppc_v1.3_win`)

```text
native: OpenCL NVIDIA GeForce RTX 3070 · OpenMP 8 · ~32 GB RAM
pipeline: capacity · effort 0.55 · DeepPCB 80/20 hybrid
benchmark_wall_s ≈ 5899 (~98 min)

80_20 routine_2pin: parallel 10/20 + serial residual 1/10  → ~11/20  (pre–fine residual)
80_20 mid_3to6:     46/59
80_20 heavy_multipin: 1/6
completion_recovery: +1 two-pin salvage; multipin restore 8/8
result: 59/85 · grade D 39.41 · hard DRC 0 · AR 369 segs / 110 vias / 778 mm
open (26): power/GND, CH/DAC subset, SPI, GPIO, 2 local_rc
human: 1199 segs / 155 vias / 1932 mm / 61 pours · AR pours: 2
```

Artifacts: `viewer/runs/mppc_v1.3_win/` on desktop host (or local `viewer/runs/mppc_v1.3/`).

---

## Test policy (prefer segments)

Full mppc capacity runs are **~1–2 h**. Prefer **segment microbenches**
(seconds–minutes) before re-running the full golden:

```bash
PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment local_rc
PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment 2pin
PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment analog
PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment hspeed
# Full golden (slow)
python scripts/run_mppc_benchmark.py
```

### Measured segment microbenches (Mac M3, empty board)

| Segment | Result | Wall | Notes |
|---------|--------|-----:|-------|
| `local_rc` | **6/6 (100%)** | 0.5 s | Dense RC pairs are fine on empty board |
| `2pin` sequential | 12/20 → fine 0.10 → **15/20** | ~5 min | Open: GPIO17/18/23, LED-0, `~FPGA_{RST}` |
| `2pin` via 80/20 + residual 0.10 | **16/20 (80%)** | **15 s** | Residual recovered 7/11; fine 0.08: +0 |
| `analog` CH*/DAC* | 10/16 → fine 0.10 → **12/16 (75%)** | ~8 min | Open: CH2, CH5, DAC3, DAC6 |
| Full golden (Win) | 59/85 D 39.41 | ~98 min | Seeded mid-stage; power + buses still open |

**Insights**

1. True **`local_rc` is not the remaining 2-pin leak** — after fine residual, longer
   2-pin **GPIO / LED / FPGA** nets still fail even on empty board.
2. Fine residual at **0.10 mm** is high ROI for routine stage leftovers (shipped).
3. **Analog is 75% complete empty-board** — full-golden open CH/DAC is largely
   corridor starvation from earlier nets, not “analog is unroutable.”
4. Next high-ROI work: escape-via for long 2-pin GPIO; conflict-directed rip-up;
   matrix bundle for residual analog; pour stage for power/GND.

---

## Six levers (priority order)

### 1. Fix routine 2-pin leak (~+4–9 nets) — **partially shipped**

| Status | Item |
|--------|------|
| Done | Residual 2-pin pass @ **0.10 mm** then **0.08 mm** (`hybrid_route` 80/20) |
| Done | `_dense_grid_for_net` densifies 2-pin retries; higher expansions |
| Done | `pin_escape_fail:` notes + `scripts/microbench_segments.py` |
| Done | Completion recovery prioritizes 2-pin salvage then analog-first mid |
| Next | Directed **escape-via + layer prefer** for GPIO/LED/FPGA 2-pin |
| Done when | routine_2pin ≥ 19/20 empty; full golden local_rc ≤ 1 open |

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

### 4. Matrix bucket for analog channels (~+4–14 with #3)

`CH0–7` / `DAC*` classified as matrix when pins ≥ 3. Empty-board microbench
already **12/16**. Remaining four + full-golden opens need shared corridor plan
and conflict rip-up, not only finer grid.

### 5. Detour-ratio commit cap

Reject commit when length &gt; α × Steiner guide (α ≈ 1.6–2) → renegotiate
instead of corridor hogging (`+5V-A` 302 vs 85 mm human).

### 6. Coarse-to-fine wall time

Most minutes in fine-grid `segment_blocked`. First pass 0.2 mm; re-route
**failures only** at 0.1 mm; spend saved time on rip-up/negotiation.
(Segment microbench already uses pass1 + fine residual pattern.)

---

## Projected trajectory

| After | Completion (est.) | Grade band |
|-------|------------------:|:----------:|
| #1 complete (GPIO escape) | ~80% | C boundary |
| +#2 pours | ~86% | C |
| +#3/#4 | ~95% | B |
| +pours + KiCad clean + economy | ≥99% | A possible |

---

## UI note

Web control plane (`physics-router serve`) is **removed**. Routing opens a
**native progress window** (tkinter) unless `--no-ui`. See [USER_GUIDE.md](USER_GUIDE.md).

See also: [ROUTING_DIFFICULTIES.md](ROUTING_DIFFICULTIES.md) ·
[DEEPPCB_NOTES.md](DEEPPCB_NOTES.md) · [MPPC_BENCHMARK.md](MPPC_BENCHMARK.md).
