# Design decisions and roadmap

This document records **why** the system is shaped the way it is, and what we deliberately left for later. Implementation details live in the code; literature survey lives in [RESEARCH.md](RESEARCH.md).

---

## Goals

1. **Closed loop to manufacturable copper** — not autoroute aesthetics. KiCad DRC (and ERC when a schematic exists) is the oracle.
2. **Physics-informed placement** — multi-objective cost (wirelength, loop L, IR, return path, CPX match, EMI proxies) on **unlocked** parts only.
3. **TopoR-style free-angle routing** — topology and clearance first; no forced 45°/90° preferred directions. Product model: [docs/TOPOR.md](docs/TOPOR.md). Architecture (topology + sparse graph + geometry, negotiated congestion): [docs/ARCHITECTURE_ROUTER.md](docs/ARCHITECTURE_ROUTER.md).
4. **Interactive engineering UI** — guided place → route (2D) → apply → DRC → Simulate (3D EMS), with variant compare.
5. **Native-only routing core** — the C++ core (`pr_native`) is the sole geometric router and clearance authority; Python remains the product shell (CLI, server, KiCad I/O) and policy layer.

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

### Graph topology plane

Before geometric search, `graph_theory.py` builds a board hypergraph with one
vertex per unique pad anchor and one hyperedge per multi-pin net. Deterministic
Kruskal trees minimize length plus projected crossings, while a weighted net
conflict graph is colored over the available copper layers with DSATUR. The
same indexed tree drives the abstract guide and the native C++ Prim frontier.
It is a preference, not a hard geometric constraint: native A\* may select a
different frontier edge if the planned one is blocked or layer-inaccessible.

After routing, copper is rebuilt as an embedded multilayer graph and audited
for components, cycle rank, geometric crossings, articulation points, bridges,
degree, layer usage, and per-net topology. This separates topological intent
from geometric legality while making both inspectable in route quality data.

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

**Decision:** Board coordinates stay KiCad mm. GLB is scaled m→mm when needed; the **view** applies a **Y-only flip** `(x, y) → (x, −y)` so HALO shows the hook (H1 at y=−13) at the **top** and the switch (S1 at x=−4.25) on the **left**, matching the PCB file. Do **not** use a 180° view rotation — that mirrors left/right. Footprint poses always come from `.kicad_pcb`; YAML `fixed` only marks lock flags. The board is flattened so its thin axis is world **+Z** (parallel to the ground grid).

**Footprint graphics:** Pads use KiCad form `(pad "n" smd|thru_hole shape …)` — shape is token index 3, not the mount type. Front 2D view skips pure **B.Cu** pads and draws large pads outline-only so the battery 12 mm ground does not hide the design. Silk/fab/courtyard polylines come from the footprint body in the PCB file.

**Footprint rotation:** Local→board uses `local_to_board` with **−rot** in the standard CCW matrix (matches pcbnew pad `GetPosition`). Using +rot swaps pads on ±90° parts (LEDs, MK1, S1 look 180° out). The stored `rotation_deg` in the model remains the file angle; only the geometry map negates it.

**Pad shape orientation:** Pad *centers* use `local_to_board`; pad *rectangles* are rotated in **board** space by **−pad_rot** (`pad_corners_board`). Applying pad rot in footprint-local space *before* place double-counts the footprint angle and leaves pads 90° off (S1 bars vertical, LED aspect swapped).

### 3c. Post-connect free-angle re-geometry (TopoR metrics)

**Decision:** After connectivity (MST + free-angle + vias), run `post_connect_regeometry`: subdivide long edges, repel vertices from foreign copper (spacing field), optionally replace sharp corners with arc chord samples. Default search grid **0.1 mm**; vias kept unless `via_minimize`/`aggressive` is explicitly on.

**Why:** Pure LOS completion produces “thick straight sticks.” Eremex-style quality is topology first, then continuous re-geometry for bends, packing, and equal spacing — electrical rules over via count.

**Metrics** (`quality.topor_geometry`): `bend_count`, `multi_bend_nets`, `arc_corners`, `min_spacing_mm`, `total_length_mm`, `via_count`. Tests: `tests/test_regeometry.py`, `tests/test_routing_quality.py`.

**Edge.Cuts:** Classic arcs are `(gr_arc (start cx cy) (end x y) (angle deg))` where `start` is the **center**, `end` is the **arc start point**, and `angle` is the CCW sweep (verified against pcbnew `GetArcStart`/`GetArcEnd`). A synthetic origin-centered circle at the main disk radius (HALO ≈12 mm, not the hook tip ≈13.6 mm) is used only for substrate fill; stroke uses the arc polylines so the teardrop/hook stays open.

**Parity tooling:** `scripts/render_viewer_2d.py` mirrors `viewer/index.html` transforms; `kicad-cli pcb export svg` references live in `docs/images/viewer_compare/`; `tests/test_viewer_kicad_parity.py` locks landmarks (H1 top, S1 left, LED +4° CW, outline chain).

**Why:** Re-centering the GLB without matching route space caused overlays to drift; edge-on GLB exports made the PCB look vertical; a full 180° view put S1 on the wrong side of U1; wrong arc sweep produced floating “wing” outline segments.

### 3b. 2D for routing; 3D only after route for EMS

**Decision:** The main left visualization is **2D** for Setup / Place / Route / Validate. **Three.js 3D** loads only on the **Simulate** step (KiCad GLB + OpenEMS EMI volumes). Route apply never rebuilds GLB from the Route panel.

**Why:** Live TopoR feedback is a copper map problem, not a 3D CAD problem. Keeping 3D out of the routing loop cuts load cost and makes the control plane match engineer mental model: route → apply → then EMS viz.

### 4. Lock product geometry; place only free parts

**Decision:** HALO uses `lock_ref_prefixes: ["D"]` plus fixed mechanicals. SA only moves unlocked footprints.

**Why:** LED ring, battery, pogo, and hook are product constraints. Global free placement is unrealistic for wearables.

### 5. C++-only geometric router (no Python fallback)

**Decision:** All clearance queries and path search run in the C++ core. `ExactMap` is the exact authority; the whole-board `GridMap` fast path uses clearance-correct centerline inflation, layer-aware pads, true Edge.Cuts occupancy, and atomic full-net transactions. Python selects buckets and validates the native result but does not implement a second geometry search. `pr_native` is required — the router raises with build instructions if missing.

**Why:** Two parallel router implementations drifted. Native v1.7 evaluates bounded bucket orders concurrently and routes the documented HALO snapshot in about 3.2 seconds. Under strict pad-layer reachability it completes 9/23 nets with zero native DRC violations. The earlier 17/23 native number was invalid because inner-layer tracks at front-only SMD pads were counted as connected without vias; the Python multipin experiment reached 19/23 under its older model but took 625 seconds.
### 6. Multi-net IC keepouts are not solid discs

**Decision:** Obstacles are built from real pad XY, oriented size, net ownership, and copper layers. Track-width and clearance inflation happen once in C++. Multi-pin package bodies do not become net-agnostic copper blocks.

**Why:** A solid keepout at U1 covered its own escape anchors and made every signal impossible. Axis-aligned boxes also joined diagonal fine-pitch pads into a false wall. Oriented pad ownership lets the attached net fan out while foreign nets keep clearance; F.Cu SMD pads no longer block inner layers, and any inner-layer escape now requires explicit vias.

### 6a. Power and ground may use refillable copper areas

**Decision:** The native core can emit rounded, Edge.Cuts-bounded `CopperArea` polygons for power/ground nets. Export writes tagged KiCad zones; KiCad owns fill, clearances, thermals, and final zone DRC.

**Why:** Organic boards need copper regions as well as centerline tracks. Treating planes as many wide traces wastes routing channels and still does not model thermal connections correctly.

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
| Multi-net paint order | Parallel native bundle variants | Exact DRC gate; select most legal completion |
| Full HALO clearance | ~3.2 s documented run | Bounded bucket orders on up to four C++ workers |
| GLB export | Seconds (kicad-cli) | Cache under `viewer/assets/` |
| OpenCL batch clearance | Validation / samples | Optional; CPU fallback always |

Apple Clang often lacks OpenMP; OpenCL on Apple GPU still helps batch checks. Linux builds can enable both OpenMP and OpenCL/CUDA later.

---

## Future improvements

Prioritized by impact on **legal, manufacturable** boards for HALO-class density.

### Near term

1. **True CPX concurrent topology** — replace order variants with a multi-commodity/bundle group and length matching; eight HALO matrix nets remain open.
2. **Filled-zone import** — read KiCad's refilled polygons back into native exact DRC and SI/current-density analysis.
3. **DRC-driven local rip-up** — parse filled-copper violations and re-route only the offending topology cluster.
4. **Push-aside geometry** — move neighboring legal traces together instead of discarding a full dense net.
5. **Pad layer legality** — distinguish SMD anchor layers from through-hole barrels in the connectivity proof.

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
