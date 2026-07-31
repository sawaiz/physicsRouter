# Routing difficulties & how to raise golden grades

**TL;DR:** Grades are driven almost entirely by **completion vs human copper**
with **0 hard DRC**. Open nets beat shorts. Latest mppc v1.3 desktop golden:
**69.4% / grade D (39.41)** with clean DRC (was F/18 at 48%). The gap is still
*which nets never found a legal path*, not aesthetics. Six-lever plan:
[SCORE_ROADMAP.md](SCORE_ROADMAP.md).

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

### Latest: Windows RTX 3070 (~98 min, effort 0.55, DeepPCB 80/20)

| | Human | AR |
|--|------:|---:|
| Complete nets | 85 | **59** (69.4%) |
| Hard DRC | 0 | **0** |
| Segments / vias / length | 1199 / 155 / 1932 mm | 369 / 110 / 778 mm |
| Areas (pours) | **61** | **2** |
| Grade | golden | **D (39.41)** |

Staging notes: routine 2-pin ~11/20 · mid 3–6 **46/59** · heavy **1/6** · recovery +1.

### Earlier Mac baseline (~28 min, effort 0.45)

| | Human | AR |
|--|------:|---:|
| Complete nets | 85 | **41** (48%) |
| Hard DRC | 0 | **0** |
| Segments / vias / length | 1199 / 155 / 1932 mm | 318 / 94 / 805 mm |
| Areas (pours) | **61** | **3** |
| Grade | golden | **F (18.24)** |

### What still fails (latest D run)

| Category | Impact | Examples |
|----------|--------|----------|
| **power_gnd** | high | `GND`, `+5V`, `+3V3`, `HV`, `+5V-A` |
| **analog_channel** | medium | subset of `CH*` / `DAC*` (empty-board analog is 75%!) |
| **digital_bus** | medium | `SCLK`, `MOSI`, `MISO`, `CLK`, `~CS_*` |
| **gpio** / LED | medium | `GPIO18`…, `LED-0` |
| **local_rc** | low (2 open) | `Net-(R2-Pad2)`, `Net-(C12-Pad1)` — empty-board local_rc is **100%** |

**Power / pour gap:** human GND alone is **521 mm** of copper + large zones;
AR left GND with **0** tracks. Power rails that do commit often **over-length**
vs human (corridor hogging). Config weights for `+5V`/`+3V3`/GND are set in
`examples/mppc-interface/placement_config.yaml`.

### Segment microbenches (empty board — use these to iterate)

| Segment | Result | Insight |
|---------|--------|---------|
| `local_rc` | 6/6 @ 0.5 s | Not a hard problem in isolation |
| `2pin` + 0.10 residual | 16/20 @ ~15 s | Residual shipped; remaining = long GPIO/LED/FPGA |
| `analog` | 12/16 @ ~8 min | Channels are routable; full golden starves them |

```bash
PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment 2pin
PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment analog
```

### Pipeline evidence (from notes)

1. **DeepPCB 80/20** stages by pin count (not classic power/critical/matrix order).  
2. **Routine 2-pin** parallel wave + **fine residual 0.10/0.08 mm** (lever 1 partial).  
3. **Mid 3–6 pin** carries most of the grade (46/59 on latest golden).  
4. **Manufacturing gate:** 0 native hard DRC with open nets (policy OK).  
5. **Global capacity:** overflow residual still high; mesh_overflow_nodes thousands.  
6. **CH/DAC** classified matrix when pins ≥ 3; still need corridor negotiation.

### Runtime difficulties (engineering)

| Issue | Symptom | Mitigation (done / next) |
|-------|---------|---------------------------|
| Hard process deadline | Worker killed mid-net → no `ar_route.json` | `timeout_s=0`, `hard_deadline=False` on mppc script |
| ThreadPool order variants + PathFinder | GIL thrash, 20+ min no copper | Cap variants / skip negotiated congestion when nets ≥ 70 |
| Native ExactMap cost | Long `segment_blocked` at fine grid | Coarse-to-fine residual; log stage `elapsed_s` |
| Empty rip-up | Notes show `ripup(empty)` | **Next:** conflict-directed victims (SCORE_ROADMAP #3) |
| Zone under-use | 2 vs 61 areas | **Next:** pour stage (SCORE_ROADMAP #2) |
| 2-pin residual | Was ~11/20 routine | **Done partial:** 0.10 residual → 16/20 empty-board |

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

### 5. Strategy mis-bucket (matrix / analog)

**Cause:** Historically CH*/DAC* treated as general; corridors sealed by early nets.

**Improve:**

- Auto matrix when pin count ≥ 3 and name matches CH/DAC (done in `classify_board`).  
- Finer grid (0.15) + shared corridor plan for channel fanout.  
- Empty-board analog microbench already 12/16 — push remainder with lever #3/#4.

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

| Goal | Status / what must change |
|------|---------------------------|
| **D (~35)** | **Reached** at 69.4% / 39.41 (Win desktop) |
| **C (~55)** | ~85% — finish 2-pin GPIO, remaining analog, SPI; pours start helping |
| **B (~75)** | ~95% — pours for GND/power, conflict rip-up, low bloat |
| **A (~90)** | ≥99% + length/via near human + KiCad DRC clean |

Detailed levers: [SCORE_ROADMAP.md](SCORE_ROADMAP.md).  
Policy never changes: **0 hard DRC** is required at every step.

### DeepPCB 80/20 (2026 essays)

See **[DEEPPCB_NOTES.md](DEEPPCB_NOTES.md)**. On boards ≥50 nets we stage:

1. **routine 2-pin** (busywork majority)  
2. **mid 3–6 pin**  
3. **heavy multipin** (GND / power / dense)  
4. **completion recovery** for remaining small open nets  

This matches DeepPCB’s claim that traditional static “power first” heuristics destroy global resource allocation.

Related: [MPPC_BENCHMARK.md](MPPC_BENCHMARK.md) ·
[AUTOROUTER_FAILURE_ANALYSIS.md](AUTOROUTER_FAILURE_ANALYSIS.md) ·
[GOLDEN_CORPUS.md](GOLDEN_CORPUS.md) · [CAPACITY_MESH.md](CAPACITY_MESH.md).
