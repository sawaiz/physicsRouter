# physicsRouter

Physics-aware **KiCad placement and TopoR-style free-angle routing** with closed-loop **DRC/ERC**, an interactive control plane, and a required **C++/OpenCL** router core (the only geometric router — no Python fallback).

Inspired by [TopoR](https://en.wikipedia.org/wiki/TopoR) / [Eremex TopoR](https://www.eremex.com/products/topor/) (gridless free-angle topology) and multi-objective placement research that scores **post-route and physical** quality, not HPWL alone.

| Doc | Contents |
|-----|----------|
| **[DESIGN.md](DESIGN.md)** | Architecture, design decisions, future work |
| **[RESEARCH.md](RESEARCH.md)** | Algorithm survey and bibliography |
| **[docs/TOPOR.md](docs/TOPOR.md)** | Eremex TopoR product model, images, manuals, binary catalog |
| **[docs/ARCHITECTURE_ROUTER.md](docs/ARCHITECTURE_ROUTER.md)** | Topology-first architecture (3 representations, congestion, roadmap) |
| **[docs/HYBRID_ROUTING.md](docs/HYBRID_ROUTING.md)** | Auto multi-strategy free-angle (matrix / power / critical / general) |
| **[docs/JLCPCB_4LAYER.md](docs/JLCPCB_4LAYER.md)** | JLCPCB 4-layer DRC/ERC floors and stackup defaults |
| **[DATASETS.md](DATASETS.md)** | Training corpora and conversion paths |

### TopoR-style routing (what we implement)

Matches the reasoning in [docs/TOPOR.md](docs/TOPOR.md) and [Eremex TopoR autorouting advantages](https://www.eremex.com/products/topor/competitiveadvantages/autorouting/) — **not** a reimplementation of commercial TopoR binaries:

| Phase | Behavior |
|-------|----------|
| **Isotropic free-angle (C++)** | Native OpenMP/GPU core: LOS → detours → A\* (JLCPCB 4L DRC floors by default) |
| **K-homotopy** | Up to K topologically distinct paths per connection (signature dedupe) |
| **High-level planner** | Feature linear policy for net order + per-net K |
| **CBS conflict clusters** | Conflict graph → small-component re-route; vias for connectivity |
| **Post-connect re-geometry** | After nets connect: **subdivide → spacing repulsion → optional arc chords** (`regeometry.py`) |
| **Elastic geometry** | Continuous shortening + obstacle repulsion (Dayan/TopoR elastic) |
| **SI / MFG costs** | Crosstalk parallel-run, return path, acute angles, via-near-pad, … |
| **Why this via** | Each via stores blocked layers + alternatives; UI explain panel |
| **Honesty policy** | Soft illegal copper **off** — open edges beat overlaps |
| **Always-on router DRC** | Native exact check: shorts, spacing, via clearance, **Edge.Cuts escapes** after every route; polish (re-geometry/elastic) is **reverted if DRC worsens** |
| **Via policy** | **Connectivity / clearance first**; via-minimize off by default |
| **UX** | Live **2D** copper while routing; **3D EMS** only on Simulate |

#### Post-connect free-angle re-geometry (why traces should bend)

Shape-based routers often leave **straight LOS sticks**. TopoR-style quality needs a second stage once topology is fixed:

1. **Subdivide** long segments (multi-bend DOF)  
2. **Spacing field** — push vertices away from foreign copper (equalize gaps; critical for pairs)  
3. **Arc-approximate** sharp corners with free-angle chord samples (visual + packing)  
4. Report **TopoR metrics**: bend count, multi-bend nets, min edge spacing, arc corners, length, vias  

Implemented in `src/physics_router/regeometry.py`, wired into `topor_style_route` / `_apply_drc_geometry`. Routes also stay inside the **Edge.Cuts** outline polygon (not just the AABB) via `ObstacleMap` / native `ExactMap`.

#### Routing process (HALO-90 renders)

Regenerate figures + DRC report:

```bash
python scripts/render_routing_process.py --halo
# full TopoR + always-on DRC map also written by the eval block in docs:
# docs/images/routing_process/{1..7}_*.png , drc_report.json
```

| Stage | Figure |
|-------|--------|
| Placement + Edge.Cuts outline | ![placement](docs/images/routing_process/1_placement_outline.png) |
| Guide / free-angle topology sketch | ![guide](docs/images/routing_process/2_guide_topology.png) |
| Clearance-aware connectivity (raw) | ![raw](docs/images/routing_process/3_clearance_raw.png) |
| Post-connect re-geometry (bends + arcs) | ![regeo](docs/images/routing_process/4_regeometry.png) |
| Copper by layer | ![layers](docs/images/routing_process/5_by_layer.png) |
| **Always-on router DRC map** | ![drc](docs/images/routing_process/7_drc_map.png) |

**Pipeline strip** (placement → guide → clearance → re-geometry):

![TopoR-style routing process](docs/images/routing_process/6_process_strip.png)

#### HALO-90 route evaluation (historical batch run · pre zero-violation gate)

Snapshot from an earlier full `topor_style_route` on `third_party/halo-90` (planner + 2 variants, grid 0.5 mm, clearance 0.15 mm, re-geometry + elastic). Useful as a **connectivity ceiling** reference; it still carried shorts. For the **zero-violation sequential policy** and lessons, see **Policies that matter** and **Lessons learned** below. Source: [`docs/images/routing_process/drc_report.json`](docs/images/routing_process/drc_report.json).

| Metric | HALO-90 | Synthetic demo |
|--------|---------|----------------|
| Completion | **23/23 nets** (0 unrouted) | 7/7 nets |
| Segments / length | 162 · 746.5 mm | 29 · 154.7 mm |
| Vias | 22 | 6 |
| Grade / score | **D · 42.4** | **A · 95.2** |
| **Router DRC total** | **38** | **0** |
| → shorts (copper touch) | 25 | 0 |
| → spacing (clearance) | 13 | 0 |
| → **outside Edge.Cuts** | **0** | 0 |
| Max copper radius | 12.26 mm (inside teardrop) | n/a |
| Bends / multi-bend nets | 156 · 14 nets | 20 · 6 nets |
| Wall time | ~59 s | ~0.8 s |

**What the images show**

1. **Outline keepout works** — DRC reports **0 outline escapes**; copper stays inside the teardrop (hook/H1 included). The earlier AABB-only bug (45 endpoints past 12 mm) is fixed.
2. **Routing is free-angle and multi-layer** — CPX nets form ring/spoke geometry; F.Cu / In\* / B.Cu color in the DRC map; vias (gold) at layer transitions.
3. **This snapshot is not DRC-clean** — most violations are **CPX-n × CPX-m** crossings near the LED ring and U1 fanout. The current router **refuses to commit that copper** (open > short); see lessons learned.
4. **Grade D was intentional honesty** for that run — score tracks real violations. Prefer 0 shorts with some unrouted nets over a high completion score with shorts.
5. **SI/MFG** (same run): ~11 mm parallel-run proxy, 14 acute angles, 22 via-near-pad hits — further polish targets, not outline leaks.

**DRC samples (HALO)** — foreign-net contact on F.Cu / In2.Cu:

| Kind | Nets | Layer | Dist / need (mm) |
|------|------|-------|------------------|
| short | CPX-0 × CPX-8 | F.Cu | 0 / 0.28 |
| short | CPX-2 × CPX-7 | F.Cu | 0 / 0.28 |
| short | CPX-4 × CPX-9 | In2.Cu | 0 / 0.28 |
| spacing | CPX-5 × CPX-7 | In2.Cu | 0.16 / 0.28 |

**DRC map** (red × short, orange × spacing, gold ● via) — regenerate with the process script + eval:

![HALO-90 router DRC map](docs/images/routing_process/7_drc_map.png)

```bash
# CLI — isotropic TopoR pipeline (auto multi-variant by net count)
physics-router route --config placement_config.yaml --pcb board.kicad_pcb \
  --out route.json --out-pcb routed.kicad_pcb --variants 2

# Quality tests (bends, clearance, re-geometry, outline bounds, router DRC)
pytest tests/test_routing_quality.py tests/test_regeometry.py \
  tests/test_outline_bounds.py tests/test_router_drc.py tests/test_topor_style.py -q
```

See [docs/ARCHITECTURE_ROUTER.md](docs/ARCHITECTURE_ROUTER.md) for the full three-representation design and literature map.

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Required: the C++ router core (OpenCL GPU when available).
# Auto-discovered from native/build — no PYTHONPATH needed in a dev checkout.
bash scripts/build_native.sh

# Control plane (default board: HALO-90 if cloned)
physics-router serve --port 8765
# → http://127.0.0.1:8765/

pytest
python scripts/ci_regression.py
```

### CLI essentials

```bash
physics-router init-config -o placement_config.yaml
physics-router import-nets --pcb board.kicad_pcb --project-dir . -o placement_config.yaml
physics-router place --config placement_config.yaml --pcb board.kicad_pcb --out-pcb placed.kicad_pcb
physics-router route --config placement_config.yaml --pcb placed.kicad_pcb --out-pcb routed.kicad_pcb --drc
physics-router drc --pcb routed.kicad_pcb --out-dir drc_out
physics-router export-step --pcb routed.kicad_pcb -o board_sim.step
physics-router export-dsn --config placement_config.yaml -o board.dsn
```

### HALO-90 test board

```bash
git clone git@github.com:openKolibri/halo-90.git third_party/halo-90
physics-router score \
  --config examples/halo-90/placement_config.yaml \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb
```

- **90 LEDs locked** via `lock_ref_prefixes: ["D"]` (product geometry).
- 4-layer stackup read from KiCad; regions and net weights in `examples/halo-90/`.

---

## What it does

```
YAML / KiCad labels  →  multi-objective place (SA, unlocked parts)
                     →  TopoR pipeline (isotropic free-angle · multi-variant · rubberband)
                     →  write copper to .kicad_pcb
                     →  kicad-cli DRC (+ ERC if schematic present)
                     →  Simulate: GLB 3D + OpenEMS EMI visualization
```

**Policies that matter**

1. Clearance routes do **not** paint illegal straight “soft” copper; open edges beat overlaps.
2. Legal routes commit **one net at a time** by weight/priority with **exact DRC after each edge/net**. Shorts and spacing hits are never kept.
3. **Full-net commit only** — partial MST stubs are dropped (open > incomplete copper that blocks later nets).
4. **Rip-up** of lower-priority conflict nets, plus **equal-weight matrix/CPX peers** when space-starved.
5. Official **KiCad DRC** remains the post-apply legality oracle; built-in `native_drc_check` is the in-loop gate.
6. **Routing UX is 2D** (KiCad-style layers). **3D is post-route** on the Simulate step for EMS/OpenEMS.
7. Routing is **isotropic free-angle** (TopoR-style), not Specctra preferred H/V.
8. 2D preview, 3D GLB, and routes share **KiCad millimetre XY** (view may Y-flip for display: hook top, switch left).
9. The C++ `pr_native` core is the geometric search engine and clearance authority (`ExactMap`). Python owns net order, full-net DRC gates, rip-up, multipin MST fallback, polish, reporting.

### Lessons learned (zero-violation sequential · HALO-90)

These came out of enforcing “no shorts even early” on the dense charlieplex earring board (`examples/halo-90/`, `third_party/halo-90`).

| Lesson | Detail |
|--------|--------|
| **Open > short is correct but changes the scoreboard** | Prior batch routes could show ~23/23 nets and ~747 mm copper with **~25 shorts**. Sequential zero-violation keeps **0 shorts** but may leave nets unrouted; grade/score will drop until multipin search catches up. Do not reintroduce soft fill to “look complete.” |
| **Partial stubs poison the board** | Committing a few legal MST edges of a 19-pin CPX net without finishing the tree paints keepouts that block peers and critical nets. **Only commit fully connected nets** (union-find over pin anchors + copper). |
| **Native post-rubberband can destroy multipin trees** | Exclusive one-net native calls with `post_rubberband=True` sometimes collapsed a valid multi-edge MST to a single segment while still reporting `ok`. Disable rubberband for multipin exclusive runs; verify connectivity in Python. |
| **Grid legality ≠ exact DRC** | Obstacle-map search can accept geometry that exact `drc_check` later rejects. Gate **every edge and every net** with `native_drc_check`; never trust paint-time grid alone. |
| **Equal-weight CPX needs peer rip-up** | Same weight (4.0) matrix lines cannot rip each other under “lower priority only.” Allow **equal-weight matrix/multipin peer rip** or they deadlock after the first few CPX commits. |
| **Do not mass-rip matrix for lower nets** | Aggressive recovery that strips all CPX to free space for MIC/I2C thrashing destroys good matrix copper. Non-matrix nets must not rip matrix; matrix rebuild should re-route the matrix class as a block. |
| **Multipin prefers Python edge-by-edge MST** | For 3+ pin / CPX nets, free-angle Prim MST in Python (with per-edge DRC) completes trees more reliably than a single exclusive native batch. Use native first for 2-pin nets; python-first for multipin. |
| **Hybrid phase order should follow weight** | Route **power → critical → matrix → general**, not matrix first. High-weight rails and short signals should claim corridors before dense multipin. |
| **Dense multipin recovery is necessary but slow** | Finer grid (≤0.15 mm), higher A* expansions, and limited peer rip improve CPX completion; full HALO zero-violation passes can take minutes. Snapshot metrics: `examples/halo-90/zero_violation_run.json`. |
| **Synthetic boards are the regression floor** | Example config board should stay **0 shorts, all nets fully connected**. HALO is the stress case (90 LEDs, 10× ~19-pin CPX). |

**Rough HALO comparison (same board, different policy)**

| Policy | Shorts | Full nets (approx) | Length | Notes |
|--------|--------|--------------------|--------|-------|
| Older batch + soft/near-miss copper | ~25 | 23/23 | ~747 mm | Looks complete; illegal |
| Sequential zero-violation + full-net commit | **0** | often ~19–23/23 | ~500–600 mm+ | Legal copper only; incomplete nets left open |

Goal: **every committed net fully matches its pin set, with zero shorts/spacing**, then raise multipin completion without relaxing the gate.

---

## Control plane

```bash
physics-router serve --host 127.0.0.1 --port 8765
```

| Step | UI |
|------|-----|
| Setup | Preset (HALO-90 / synthetic), YAML, locked vs free parts · **2D board** |
| Place | SA on unlocked footprints; physics weights · **2D board** |
| Route | Isotropic TopoR free-angle, multi-variant, **2D only** (no 3D), apply copper |
| Simulate | **3D + OpenEMS EMI** visualization, spice/PI, rebuild GLB |
| Validate | pytest, CI regression, DRC, ERC |

Assets: `viewer/` (UI), `viewer/assets/*.glb` (regenerated locally; large files gitignored).

### 2D viewer ↔ KiCad parity

The control-plane canvas matches KiCad board orientation and footprints:

| Landmark | File / view |
|----------|-------------|
| Hook **H1** | Board `y = −13` → **top** after Y-flip `(x, −y)` |
| Switch **S1** | Board `x = −4.25`, PCB rot `−90°` → **left** of U1 |
| LED ring | `+4°` board step reads **clockwise** on screen after Y-flip |
| Edge.Cuts | Classic `gr_arc` (center + start + CCW angle) → teardrop outline |
| Front pads | B.Cu-only / large pour pads hidden or outline-only |

```bash
# Install kicad-cli on PATH (macOS example)
ln -sf /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli /opt/homebrew/bin/kicad-cli

# Official KiCad layer plots → docs/images/viewer_compare/kicad_ref/
kicad-cli pcb export svg -o docs/images/viewer_compare/kicad_ref \
  --mode-multi --layers F.Cu,B.Cu,F.SilkS,Edge.Cuts,F.Fab \
  third_party/halo-90/pcb/halo-90.kicad_pcb
# PNG via scripts/generate_kicad_renders.py or rsvg-convert

# Headless 2D render (same transforms as viewer/index.html)
python scripts/render_viewer_2d.py -o docs/images/viewer_compare/viewer_2d.png

# Automated landmarks + footprint/outline checks
pytest tests/test_viewer_kicad_parity.py -v
```

Compare assets live under [`docs/images/viewer_compare/`](docs/images/viewer_compare/).

---

## Native C++ core (required — the only router)

| Path | Role |
|------|------|
| `native/src/exact.cpp` | **ExactMap** clearance authority (spatial hash, Liang–Barsky, painted seg–seg distance) + free-angle search (LOS · detours · radar · 1/2/3-corner · hierarchical multi-grid A\* 16-dir · rubberband) |
| `native/src/router.cpp` | Whole-board batch route (GridMap fast path), multi-site vias + reasons, via minimize |
| OpenCL | GPU batch clearance (e.g. Apple M3) |
| OpenMP | Parallel score batches when available |
| `scripts/build_native.sh` | CMake + pybind11 → `pr_native*.so` |
| `./native/build/pr_bench` | Micro-benchmark |

```bash
bash scripts/build_native.sh
python -c "from physics_router.native_bridge import info; print(info())"
# → 1.1.0-native-isotropic · GPU when OpenCL present
```

Details: [native/README.md](native/README.md). Python owns policy and polish only (K-homotopy / CBS / planner / elastic / regeometry / SI-MFG); every clearance query and path search runs in C++. Without `pr_native` the router raises with build instructions.
---

## Architecture (modules)

| Module | Role |
|--------|------|
| `models` / `config_io` | Net labels, physics weights, YAML |
| `kicad_io` / `design_rules` | Footprints, stackup, DRC floors |
| `placement` / `physics` | SA placement + multi-objective scores |
| `router` / `routing_strategies` / `topology` | Free-angle core, signatures, congestion |
| `homotopy` / `planner` / `conflict_cbs` | K-homotopy, net-order policy, CBS/CP-SAT repair |
| `elastic` / `si_mfg` | Continuous forces; SI + manufacturing cost terms |
| `native_bridge` | Optional C++/OpenCL backend |
| `kicad_tools` | DRC, ERC, STEP/GLB, renders |
| `server` / `viewer` | HTTP API + three.js / 2D UI |
| `dsn_export` / `compare` | Specctra DSN vs FreeRouting metrics |

---

## Requirements

- Python 3.10+
- KiCad 8+ (`kicad-cli`) for DRC/ERC/STEP/GLB on real boards
- Optional: Ngspice, OpenEMS/CSXCAD, CMake 3.16+ for native build

---

## License

MIT — see package metadata. HALO-90 is a separate project; clone under `third_party/` (gitignored).
