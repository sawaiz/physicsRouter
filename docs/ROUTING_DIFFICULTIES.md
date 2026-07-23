# Routing difficulties & how to raise golden grades

**TL;DR:** Grades are driven almost entirely by **completion vs human copper**
with **0 hard DRC**. Open nets beat shorts. On mppc v1.3 the AR got **48% /
grade F** with clean DRC — the gap is *which nets never found a legal path*,
not aesthetics.

Every golden / capacity run now writes:

| Artifact | Purpose |
|----------|---------|
| `viewer/runs/<id>/route_diagnostics.json` | Machine-readable failure categories |
| `viewer/runs/<id>/route_diagnostics.md` | Human summary + recommended actions |
| `viewer/runs/<id>/stage progress via pipeline elapsed_s` | Where wall time went |
| `viewer/runs/<id>/golden_compare.*` | Per-net length / via deltas |
| `viewer/runs/<id>/progress.json` | Benchmark heartbeats (`run_mppc_benchmark.py`) |

Module: [`src/physics_router/route_diagnostics.py`](../src/physics_router/route_diagnostics.py).

---

## Score math (why F at 48%)

```text
score ≈ 100 × completion  −  min(30, 5 × missing_nets)
length / via efficiency only count when completion ≥ 99%
```

| Completion | Missing | Approx score | Grade |
|-----------:|--------:|-------------:|:-----:|
| 0.48 | 44 | 48 − 30 = **18** | F |
| 0.65 | 30 | 65 − 30 = **35** | D |
| 0.85 | 13 | 85 − 30 = **55** | C |
| 0.95 | 4 | 95 − 20 = **75** | B |
| 0.99+ | 0–1 | 90–100 | A/B |

**Implication:** optimize for *more fully connected multipin nets*, never for
shorter tracks while nets are open.

---

## Case study: mppcInterface v1.3 (commit 580c61d)

Measured capacity run (~28 min, effort 0.45, dense-board hybrid caps):

| | Human | AR |
|--|------:|---:|
| Complete nets | 85 | **41** |
| Hard DRC | 0 | **0** |
| Segments / vias / length | 1199 / 155 / 1932 mm | 318 / 94 / 805 mm |
| Areas (pours) | **61** | **3** |
| Grade | golden | **F (18.24)** |

### What failed (categories)

| Category | Count | Examples |
|----------|------:|----------|
| **power_gnd** | high impact | `GND`, `+5V`, `+3V3`, `HV` |
| **analog_channel** | all open | `CH0`–`CH7`, most `DAC*` |
| **digital_bus** | SPI/FPGA | `SCLK`, `MOSI`, `MISO`, `CLK`, `~CS_*` |
| **gpio** | open | `GPIO17`…`GPIO27` |
| **local_rc** | some open | `Net-(R*-Pad2)`, `Net-(C11-Pad1)` |

**Power / pour gap:** human GND alone is **521 mm** of copper + large zones;
AR left GND with **0** tracks. The hybrid *power* phase only reserved a few
rails (`+5V-A`, `-5V`) which then **over-length** vs human (corridor hogging:
`+5V-A` 302 mm vs 85 mm human). Digital `+5V` was **missing from config**
(default weight 1.0) so it lost priority — fixed in
`examples/mppc-interface/placement_config.yaml`.

### Pipeline evidence (from notes)

1. **Critical phase** (5 nets): partial; several left unrouted.  
2. **General phase** (74 nets): native batch committed **37/74**, then
   **ripup(empty)** loops for GPIO / FPGA / DAC peers — no legal alternate
   after 3 attempts.  
3. **Manufacturing gate:** 41/85 complete, **0** native DRC (policy OK).  
4. **Global capacity:** final_overflow still ~50; mesh_overflow_nodes thousands.  
5. **No matrix bucket** on this board (power=6, critical=5, general=74) —
   multipin CH/DAC treated as general, coarser grid.

### Runtime difficulties (engineering)

| Issue | Symptom | Mitigation (done / next) |
|-------|---------|---------------------------|
| Hard process deadline | Worker killed mid-net → no `ar_route.json` | `timeout_s=0`, `hard_deadline=False` on mppc script |
| ThreadPool order variants + PathFinder | GIL thrash, 20+ min no copper | Cap variants / skip negotiated congestion when nets ≥ 70 |
| Native ExactMap cost | ~25 min in `GridMap::segment_blocked` | Fine for quality; log stage `elapsed_s`; optional coarser grid early |
| Empty rip-up | Notes show `ripup(empty)` | Need better peer selection / congestion costs / section replan |
| Zone under-use | 3 vs 61 areas | Post-legal pour stage + physics feedback |

---

## Difficulty catalog → improvements

### 1. Incomplete multipin connectivity (primary grade driver)

**Cause:** Greedy net order + zero-violation commit leaves late multipin nets
with sealed corridors; rip-up cannot invent space.

**Improve:**

- Weight config for real power/bus nets (done for mppc).  
- Few-pin-first within class; Steiner/section packing before detail.  
- Re-enable **bounded** negotiated congestion on conflict *clusters* only
  (not full 85-net ThreadPool).  
- Hierarchical CBS on overflow sections.

### 2. Power / GND without pours

**Cause:** Router emits tracks/vias; human relies on **zones**. Score treats
zone-only nets as copper the AR must match.

**Improve:**

- Zone-aware completion: grow pours for `net_class=ground/power` after legal
  stubs exist.  
- `improve --physics-feedback` after full legal copper.  
- Optionally score zone-only nets separately (still prefer real connectivity).

### 3. Corridor hogging (AR ≫ human length on early nets)

**Cause:** Early power nets detour freely under soft capacity costs.

**Improve:**

- Detour cap vs MST/Steiner guide (reject commit if length ≫ α × guide).  
- Stronger history costs on shared mesh cells.  
- Elastic regeometry pass after more nets complete.

### 4. Empty rip-up

**Cause:** Rip-up removes peers but search still finds no path (wrong layer
escape, via profile, or true blockage).

**Improve:**

- Log pin-access failures per open net (already partially in pin_access.json).  
- Layer-sequence retry from global section assignment.  
- Shared-escape vias for multipin fanout before long-haul.

### 5. Strategy mis-bucket (no matrix)

**Cause:** CH*/DAC* not classified as dense matrix multipin.

**Improve:**

- Auto matrix when pin count ≥ 3 and not power.  
- Finer grid (0.15) for channel fanout.

### 6. Global overflow residual

**Cause:** Capacity PathFinder iterations plateau with overflow > 0.

**Improve:**

- Higher effort / depth on HEP boards.  
- Feed overflow Steiner occupancy into detail edge costs more aggressively.  
- Cut preflight is feasible on mppc — use saturated-cut *warnings* to
  re-color layers even when not hard-saturated.

---

## Logging checklist (keep forever)

When debugging a grade drop, collect:

```bash
# Flagship
python scripts/run_mppc_benchmark.py
ls viewer/runs/mppc_v1.3/
# → route_diagnostics.{json,md}  golden_compare.*  ar_route.json
# → progress.json  pin_access.json  topology.json  stage elapsed in quality

# Any board
physics-router golden-eval --manifest examples/mppc-interface/manifest.yaml
```

Inspect:

1. `route_diagnostics.md` → **Recommended actions**  
2. `missing_by_category` → which subsystem failed  
3. `corridor_bloat` → who stole space  
4. `ripup.top_nets` → who needs better search / order  
5. `capacity.final_overflow` → global plan still broken  
6. Pipeline `elapsed_s` per stage → time budget

---

## Target roadmap to higher mppc grades

| Goal | What must change |
|------|------------------|
| **D (~35)** | ~65% completion — route +5V/+3V3/GND stubs + half of CH/DAC |
| **C (~55)** | ~85% — full SPI + GPIO + remaining analog |
| **B (~75)** | ~95% — pours for GND/power, low bloat |
| **A (~90)** | ≥99% + length/via near human + KiCad DRC clean |

Policy never changes: **0 hard DRC** is required at every step.

Related: [MPPC_BENCHMARK.md](MPPC_BENCHMARK.md) ·
[AUTOROUTER_FAILURE_ANALYSIS.md](AUTOROUTER_FAILURE_ANALYSIS.md) ·
[GOLDEN_CORPUS.md](GOLDEN_CORPUS.md) · [CAPACITY_MESH.md](CAPACITY_MESH.md).
