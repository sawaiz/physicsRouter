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

## Root cause of the 2-pin residual (measured 2026-08)

The residual is **not** missing escape geometry. Every remaining failure
(`GPIO17/18/23`, `LED-0`, `~FPGA_{RST}`) terminates on **U9 = ICE40-LP384
QFN32**, and each routes **grade A in isolation**; all five together route
**5/5 in 0.4 s** when given priority. The escape machinery (`pin_access` outward
`_direction_order` + QFN `_dense_radii`, native `two_via_fanout`) already
reproduces the human oracle — pin-access proposes `(-3.19, 6.16)` for GPIO17
where the human via sits at `(-3.15, 6.15)`.

They fail **only in a batch**: unconstrained nets consume the QFN pad-ring
corridor first. This is escape-resource *contention* — an allocation-order
problem, not a search-quality one.

Hard geometry behind it: U9 pitch 0.50 mm, pad width 0.25 mm ⇒ **0.25 mm gap**,
while track 0.15 + 2×0.15 clearance needs **0.45 mm**. Routing *between*
adjacent QFN pads is impossible at these rules — which is why refining
0.10 → 0.08 mm returned exactly +0. Escape must go radially outward, then via.

### Lever 1 — escape scarcity: path-dependent, ships as API only

`PinAccessPlan.escape_scarcity()` / `.order_by_escape_scarcity()` implement the
most-constrained-variable heuristic (few escapes × local rivals × package-ring
crowding).

| Path | Alphabetical | Escape-scarcity |
|------|-------------:|----------------:|
| direct native batch (`route_board_native`, exclusive) | 12/20 | **15/20** (0.7 s) |
| per-net `clearance_aware_route` (80/20 stage shape) | 15/20 | **12/20** |

It **helps the batch path and hurts the staged path**, which applies its own
priority and rip-up. It is therefore *not* wired into
`_deeppcb_eighty_twenty_route`; wire it only behind an A/B on that path. Worth
noting the batch path reaches 15/20 in **0.7 s** where the two-pass microbench
needs ~310 s for the same 15.

### Lever 2 — neck-down (shipped)

`DesignRules.escape_track_width_for_net()` + native `NetSpec.escape_width_mm`:
the pad-escape stub runs at the fab floor while the corridor keeps nominal
width, never below the DRC minimum. Verified on `+5V-A`: 35 stub segments
@0.15 mm and 21 run segments @0.30 mm. `si_mfg` no longer charges `neck_risk`
for stubs ≤1 mm — a short minimum-width escape is standard practice, not a
defect.

### Lever 3 — routability-scored placement (shipped)

`physics.escape_congestion()` scores **pad escape demand vs lane capacity**
(`Σ overflow²`, the capacity-mesh shape) instead of counting components in 5 mm
cells. It is pad-aware where `density_congestion` is component-aware: **0.0** on
the human mppc placement, **128.4** with 40 parts piled on the QFN. Weighted 2.5
in `PhysicsWeights`, so SA placement can no longer trade routability for
wirelength. This closes a gap [PCBWorld](https://arxiv.org/html/2607.05915v1)
leaves explicitly open — it fixes placement as input and never scores its effect
on routability.

Tests: `tests/test_routability_levers.py`.

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
