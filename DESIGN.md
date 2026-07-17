# Design decisions and roadmap

This document records **why** the system is shaped the way it is, and what we deliberately left for later. Implementation details live in the code; literature survey lives in [RESEARCH.md](RESEARCH.md).

---

## Goals

1. **Closed loop to manufacturable copper** — not autoroute aesthetics. KiCad DRC (and ERC when a schematic exists) is the oracle.
2. **Physics-informed placement** — multi-objective cost (wirelength, loop L, IR, return path, CPX match, EMI proxies) on **unlocked** parts only.
3. **TopoR-style free-angle routing** — topology and clearance first; no forced 45°/90° preferred directions.
4. **Interactive engineering UI** — guided place → route → apply → DRC → 3D, with variant compare.
5. **Optional native speed** — C++ core for hot paths; Python remains the product shell (CLI, server, KiCad I/O).

Non-goals (today): full commercial autorouter density, guaranteed DRC-zero on dense charlieplex without human cleanup, learned RL policies in production.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  CLI / HTTP server / viewer (Python)                        │
│  config · jobs · progress · KiCad export · GLB · DRC/ERC    │
└───────────────────────────┬─────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
  placement.py         router.py          kicad_tools.py
  physics.py      native_bridge.py?       design_rules.py
        │                   │
        │                   ▼
        │            pr_native (C++17)
        │            grid A* · OpenCL batch
        ▼
     Ngspice / OpenEMS proxies (optional binaries)
```

**Python owns** orchestration, file formats, UI, and tool invocation.  
**C++ owns** (when built) occupancy grids, A\*, multi-net MST routing, batch wirelength.  
**KiCad owns** DRC, ERC, STEP/GLB geometry, official plots.

---

## Design decisions

### 1. Soft fallback off for clearance routes

**Decision:** In clearance mode, if a path cannot be found, **do not** draw a straight illegal segment. Leave the edge open (`partial` / `unrouted`).

**Why:** Straight fallbacks produced stacked overlapping copper on HALO (star of spokes through the center). That looked “routed” but failed real DRC and misled the UI.

**Tradeoff:** Completion rate drops until the search is stronger; honesty > fake connectivity.

### 2. KiCad DRC after every apply / autoroute

**Decision:** Writing copper to `.kicad_pcb` triggers `kicad-cli pcb drc`; schematic ERC runs when a sibling `.kicad_sch` is found.

**Why:** Soft in-engine audits are approximate. Official DRC is what fab and engineers trust.

### 3. Shared millimetre XY for 2D, 3D, and routes

**Decision:** Board coordinates stay KiCad mm. GLB is scaled m→mm when needed; display may apply a **view-only 180°** so HALO matches KiCad (hook at top). The board is flattened so its thin axis is world **+Z** (parallel to the ground grid).

**Why:** Re-centering the GLB without matching route space caused overlays to drift; edge-on GLB exports made the PCB look vertical.

### 4. Lock product geometry; place only free parts

**Decision:** HALO uses `lock_ref_prefixes: ["D"]` plus fixed mechanicals. SA only moves unlocked footprints.

**Why:** LED ring, battery, pogo, and hook are product constraints. Global free placement is unrealistic for wearables.

### 5. Hybrid Python + optional native core

**Decision:** Keep CLI/server/UI in Python; accelerate routing/scoring in C++ behind `native_bridge`.

**Why:** Full rewrite would delay product features. Hot path (A\* + grids) dominates wall time; KiCad I/O does not.

### 6. Multi-net IC keepouts are not solid discs

**Decision:** Only single-net footprints get a pad keepout disc. Multi-pin ICs do not place a net-agnostic block at the origin.

**Why:** A solid keepout at U1 blocked every net in the simplified pin model (all pads share the component center).

### 7. Rubberband only on continuous polylines

**Decision:** Dayan-style cleanup chains **connected** segments only, not all edges of an MST as one path.

**Why:** Concatenating tree edges invented false mid-segments and **increased** length.

### 8. 2D preview mimics pcbnew layers

**Decision:** Canvas uses KiCad-like layer colors (F.Cu red, In1 green, In2 orange, B.Cu blue, silk yellow, Edge.Cuts).

**Why:** Engineers already know that palette; reduces cognitive load vs arbitrary net colors alone.

---

## Performance notes

| Path | Typical cost | Mitigation |
|------|----------------|------------|
| Python A\* + rect obstacles | High on dense boards | C++ `GridMap` + `pr_native` |
| Multi-net paint order | Sequential (correctness) | Priority order; better search later |
| Full HALO clearance | Seconds–minutes | Coarser grid for UI; native backend |
| GLB export | Seconds (kicad-cli) | Cache under `viewer/assets/` |
| OpenCL batch clearance | Validation / samples | Optional; CPU fallback always |

Apple Clang often lacks OpenMP; OpenCL on Apple GPU still helps batch checks. Linux builds can enable both OpenMP and OpenCL/CUDA later.

---

## Future improvements

Prioritized by impact on **legal, manufacturable** boards for HALO-class density.

### Near term

1. **Stronger same-layer clearance** — continuous geometry (not only grid samples); push-aside cleanup.
2. **CPX concurrent bundle** — route charlieplex as a multi-commodity group with length match.
3. **Rip-up and reroute** — conflict learning: nets that fail get higher priority next pass.
4. **Pad-accurate anchors** — use real pad offsets from KiCad, not component centers.
5. **DRC-driven rip-up** — parse copper violations and locally re-route offenders.

### Medium term

6. **IR / current-density maps** on copper polygons (drives width and layer choice).
7. **Return-path continuity** as a hard constraint on layer hops.
8. **Via budget auction** — limited vias assigned by net criticality.
9. **Golden copper diff** vs released HALO-90 for honest autorouter metrics.
10. **Metal GPU** path on Apple (OpenCL is deprecated long-term).

### Longer term

11. **RL / imitation** with physics + DRC reward (DATE/RL_PCB style).
12. **GNN affinity** floorplanning before SA.
13. **Constraint DSL** — `keep MIC 3mm from CPX*`, `match CPX-* ±5%`.
14. **Diff-pair co-router** as a rigid pair move.

### Explicitly deferred

- Full NS-Place MILP legalization  
- Blind/buried via optimization  
- Production sign-off without human review  

---

## Testing strategy

| Layer | What |
|-------|------|
| Unit | Router extent, soft-fallback policy, rubberband, append/strip copper, native bridge |
| Integration | HALO load + locked LEDs, DRC/ERC when `kicad-cli` present |
| API | Serve jobs: score, route_guide, apply_route_pcb, route select |
| Regression | `scripts/ci_regression.py` baselines for score / guide length |

Run:

```bash
pytest
# with native:
PYTHONPATH=native/build:src pytest
```

---

## Repository layout

```
src/physics_router/   # Python package
native/               # C++17 core (CMake)
viewer/               # Control-plane UI + assets
examples/             # configs, HALO labels, demo outputs
tests/
scripts/              # build_native, CI, image generators
docs/images/          # figures for README / research
```

External boards live in `third_party/` (gitignored). Large GLB/STEP under `viewer/assets/` are regenerated locally.
