# physicsRouter

`physicsRouter` is a research PCB autorouter for KiCad. Its target is a
topology-first, free-angle router that produces physically connected copper,
explicit vias and refillable power areas, then proves the result with KiCad
DRC. The C++ core owns geometric search; Python owns KiCad I/O, routing policy,
validation, scoring and the control plane.

The project is not fabrication-ready on the HALO-90 stress board yet. The
important result today is an honest diagnosis of why, plus a concrete path from
the current sequential router to a graph-planned, congestion-negotiating one.

## Project status

| Code state | What it demonstrates | Limitation |
|---|---|---|
| `main` source · native `1.1.0-native-isotropic` after a fresh build | Free-angle search, sequential routing, KiCad apply/DRC, live UI | Pad layers and pad obstacles are modeled incorrectly; HALO routes can contain impossible layer connections and shorts |
| `codex/native-organic-router` · native `1.8.0-graph-topology` | Hypergraph trees, conflict-graph layer coloring, explicit SMD escapes/vias, oriented pad obstacles, atomic nets and organic areas | Native-DRC-clean but incomplete on HALO; dense CPX nets need a concurrent bundle/global solver and final KiCad sign-off |

### Reproduced HALO-90 failure on `main`

A 4-layer improve run on 2026-07-19 made the failure measurable:

The monitored server had loaded a stale `1.2.0-native-atomic` module; rebuilding
current `main` produces `1.1.0-native-isotropic`. The route is useful failure
evidence, but it is not reproducible from `HEAD` without that missing binary
source. Runtime version and build provenance must therefore be part of every
route artifact.

| Round | Strategy | KiCad copper violations | Vias | Unrouted nets |
|---|---:|---:|---:|---:|
| 1 | hybrid | 489 | 0 | 4 |
| 2 | native | 588 | 0 | 4 |
| 3 | native fine | 588 | 0 | 4 |

Round 1 emitted 192 segments / 596.8 mm. It placed 50 segments on `In1.Cu`
and 18 on `In2.Cu` while emitting no via objects, even though most HALO pads
are front-side SMD pads. KiCad reported real shorts including `TX`–`GND` and
multiple CPX-to-CPX conflicts. The in-process audit simultaneously reported
zero shorts/spacing/outline errors, proving that its collision model is not
equivalent to the board written for KiCad.

This is not a rendering artifact and it is not fixed by a finer grid.
The 1,200-second run stopped after 1,288 seconds because the deadline is only
checked between blocking native calls. Its returned summary also reset
`clearance_violations` to zero while selecting the 489-violation route.

## Why the current autorouter fails

1. **Layer-blind pin access.** Native v1.2 lets an F.Cu-only SMD anchor start or
   finish on an inner layer. A colored line exists in XY, but there is no
   electrical connection without a via and an outer-layer escape.
2. **The wrong obstacle model.** The bridge approximates a component body with
   one rectangle instead of painting every rotated pad on the copper layers it
   occupies. Tracks can therefore cross a foreign-net pad that KiCad correctly
   treats as a short.
3. **Incomplete connectivity semantics.** Completion is inferred from generated
   segments rather than connected components in a multilayer graph containing
   pads, tracks and via spans. Multipin partial trees can look routed and poison
   later nets.
4. **The fast DRC omits important object pairs.** Track–track checks are not
   enough. Track–pad, via–pad, hole clearance, zone fill, layer reachability and
   dangling/unconnected copper must agree with KiCad.
5. **Via placement is an emergency fallback, not a global plan.** Layer
   assignment and via sites need to be chosen with pin access and projected
   crossings. Assigning a net to multiple layers without transition vertices is
   invalid.
6. **Sequential blocking is too greedy for the CPX ring.** Early matrix nets
   consume corridors; later peers cannot negotiate for them. Re-running the
   same broken model at a finer grid can increase violations, as round 2 did.
7. **The improvement loop is not sufficiently diverse.** Grid and net-order
   variants share the same faulty physical model, so the score search cannot
   escape the failure class.
8. **Result bookkeeping is inconsistent.** KiCad violations are folded into a
   temporary score but are not reliably retained in the returned route, so the
   API can expose `clearance_violations=0` for a known failing candidate.
9. **Long native calls hold the Python GIL.** During the monitored route the
   server stopped answering progress requests while the process used a full CPU
   core. Cancellation, deadlines and UI updates cannot be dependable until the
   binding releases the GIL or routing runs in a worker process.
10. **The loaded native binary can drift from source.** Python imported a stale
   v1.2 module while the same checkout rebuilt as v1.1. Results cannot be
   compared unless the native source commit, binary version and build options
   are recorded and checked together.
11. **Power-area connectivity is separate from area shape.** A visually organic
   polygon is useful only after pads have legal track/via access and KiCad has
   refilled thermals and verified the resulting copper.

## What established routers do differently

| Scheme / example | Core behavior | Lesson for physicsRouter |
|---|---|---|
| **Maze / line search** (Lee, Hadlock, A*) | Searches a discretized routing graph; strong reachability foundation but grid quality and ordering dominate | Keep A* as a geometry primitive, not the whole autorouter |
| **Shape-based interactive routing** ([KiCad PNS](https://docs.kicad.org/master/en/pcbnew/pcbnew.html)) | Walk-around or shove preserves local DRC; pads and locked copper are immutable obstacles; layer changes create explicit vias | Make the same board-object and rule model authoritative during search, not only after export |
| **Batch maze + rip-up/optimization** ([FreeRouting](https://github.com/freerouting/freerouting)) | DSN/SES interchange, repeated routing/optimization passes, DRC, partial output and stagnation detection | Add reproducible whole-board passes, useful stopping criteria and a real external baseline |
| **Topology + elastic geometry** ([Eremex TopoR](https://www.eremex.com/products/topor/)) | Optimizes several topology variants, delays final geometry, uses arbitrary angles and moves branches/vias while preserving topology | Separate abstract topology, layer/via assignment and physical geometry; optimize variants rather than freezing the first path |
| **Negotiated congestion** ([PathFinder paper](https://janders.eecg.utoronto.ca/1387/readings/pathfinder.pdf)) | Nets may temporarily contend for resources; present and historical costs push repeated conflicts to alternatives | Replace CPX peer deadlock with bounded global negotiation, then require a legal final state |
| **Pin access + search-and-repair** ([TritonRoute](https://openroad.readthedocs.io/en/latest/main/src/drt/README.html)) | Separates pin access, track assignment, initial detail routing, local search/repair and DRC | Treat SMD escape feasibility as a first-class phase and repair small conflict regions instead of rerouting everything |

KiCad is primarily the interactive/final-board reference, FreeRouting is the
open DSN/SES batch baseline, and TopoR is the product-model inspiration.
FreeRouting is GPL-3.0 and is used only as an external benchmark; its code is not
copied into this MIT project.

## Target architecture

```text
KiCad board + rules
        |
        v
exact pad/shape/layer model -----> pin-access graph
        |                               |
        v                               v
net hypergraph -----------------> candidate Steiner/topology trees
        |                               |
        +---- crossing/conflict graph --+----> layer coloring + via-site plan
                                                |
                                                v
                                  negotiated global routing
                                                |
                           atomic track/via geometry transactions
                                                |
                         exact in-loop DRC <----+----> local rip-up/repair
                                                |
                         zones/refill -> KiCad DRC -> score/Pareto variants
```

The useful graph-theory pieces are concrete:

- A **hypergraph** represents each multi-pin net without pretending it is a
  list of independent two-pin connections.
- Crossing-aware Steiner/MST candidates provide alternative topologies.
- A **conflict graph** captures projected tree crossings; DSATUR or another
  coloring heuristic proposes layers.
- Pads, track endpoints and vias form an embedded multilayer connectivity graph
  used to prove that every required pin belongs to one component.
- Congestion history changes edge costs between passes.
- Cut vertices, bridges and cycle rank expose fragile trees and unnecessary
  loops after routing.

Homotopy classes and crossing number are useful topological descriptors. Knot
polynomials are not a current priority: exact pin access, connectivity, DRC and
congestion negotiation must work first.

## Implementation priorities

| Priority | Work | Acceptance condition |
|---|---|---|
| **P0** | Merge the v1.8 pad-layer, oriented-pad, atomic-net and explicit-via model | No inner-layer endpoint can connect directly to an SMD pad; every layer transition has a via |
| **P0** | Make native DRC cover pads, tracks, vias, holes, outline and connectivity | Native result agrees with KiCad on a corpus of intentionally broken boards |
| **P0** | Release the GIL and add cancellation/hard deadlines | UI/API remains responsive and a timed-out native search actually stops |
| **P0** | Preserve oracle metrics through selection and API serialization | Returned route, score, viewer and KiCad report expose the same violation count |
| **P0** | Enforce native source/binary provenance at startup and in CI | Server rejects a mismatched module and every route records commit, native version and build flags |
| **P1** | Concurrent CPX bundle routing with negotiated congestion | HALO completion improves without increasing KiCad violations |
| **P1** | Explicit pin-escape and legal via-site planning | Every SMD-to-inner route has a clearance-valid escape; no dangling vias |
| **P1** | Local DRC-driven rip-up/search-repair | Only conflict clusters are rebuilt; accepted unaffected nets remain stable |
| **P1** | KiCad-oracle feedback in candidate selection | A candidate that fails KiCad copper DRC cannot become “best” |
| **P2** | Differential-pair, length/skew and return-path constraints | Pair geometry and electrical constraints are validated, not merely scored |
| **P2** | Native organic zones for power/ground | KiCad refill produces connected thermals and zero zone-related copper errors |

## Definition of “working”

A route is successful only when all of these are true:

1. Every required pad is in the correct net’s multilayer connected component.
2. Every committed net is complete; rejected nets leave no partial copper.
3. Every layer transition is represented by a legal via with a valid span.
4. Native DRC reports zero hard violations.
5. Applied/refilled KiCad copper reports zero copper DRC errors.
6. The score is computed from the same physical objects that are exported.
7. The run is deterministic for a seed, bounded in time and interruptible.

Completion, length and via count are optimization metrics only after those
invariants hold. Open nets are preferable to shorts, but an open route is not a
finished route.

## Benchmarks

- **Synthetic:** all nets complete, native DRC zero, KiCad DRC zero.
- **HALO-90:** 90 locked LEDs, 10 dense CPX nets, four copper layers; primary
  stress test for multipin topology, pad escape, vias and global congestion.
- **FreeRouting DSN/SES:** same board and rules; compare completion, KiCad
  violations, length, vias, runtime and reproducibility.
- **Released HALO copper:** golden human-routed reference for density, topology,
  length, vias and manufacturability—not a geometry-copy target.
- **Adversarial fixtures:** rotated SMD pads, blind layer access, track-through-pad,
  missing via, dangling via, narrow channel, zone thermal and Edge.Cuts cases.

Never publish a “best” score without the route artifact, native audit, KiCad DRC
report, version/commit, rules, seed and wall time.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Required C++17 router core.
bash scripts/build_native.sh
python -c "from physics_router.native_bridge import info; print(info())"

# HALO-90 is used automatically when present under third_party/halo-90.
physics-router serve --host 127.0.0.1 --port 8765
# http://127.0.0.1:8765/

pytest
python scripts/ci_regression.py
```

### CLI

```bash
physics-router import-nets --pcb board.kicad_pcb --project-dir . -o placement_config.yaml
physics-router place --config placement_config.yaml --pcb board.kicad_pcb --out-pcb placed.kicad_pcb
physics-router route --config placement_config.yaml --pcb placed.kicad_pcb --out route.json --out-pcb routed.kicad_pcb --drc
physics-router drc --pcb routed.kicad_pcb --out-dir drc_out
physics-router export-dsn --config placement_config.yaml -o board.dsn
```

## Repository map

| Path | Role |
|---|---|
| `native/` | C++ free-angle search, exact map, grid router, vias and OpenCL/OpenMP support |
| `src/physics_router/router.py` | Routing orchestration, connectivity, DRC and KiCad copper output |
| `src/physics_router/native_bridge.py` | Board-to-native geometry/layer bridge |
| `src/physics_router/topology.py` | Topology signatures and embedded-route analysis |
| `src/physics_router/graph_theory.py` | Hypergraph planning and conflict-graph layer assignment on the graph branch |
| `src/physics_router/continuous_improve.py` | Candidate loop, scoring and KiCad oracle |
| `src/physics_router/kicad_tools.py` | KiCad DRC/ERC, render and export integration |
| `viewer/` / `src/physics_router/server.py` | Local control plane and live route view |
| `examples/halo-90/` | HALO config and recorded experiments |

## Further documentation

- [Design decisions and roadmap](DESIGN.md)
- [Topology-first architecture](docs/ARCHITECTURE_ROUTER.md)
- [TopoR product-model research](docs/TOPOR.md)
- [Hybrid routing](docs/HYBRID_ROUTING.md)
- [Research bibliography](RESEARCH.md)
- [JLCPCB layer/rule profiles](docs/JLCPCB_4LAYER.md)
- [Native core](native/README.md)
- [Datasets and baselines](DATASETS.md)

The existing routing-process images are historical experiments, not proof of a
fabrication-ready route. Regenerate them with the exact version and DRC reports
used for the run:

```bash
python scripts/render_routing_process.py --halo
```

## Requirements and license

- Python 3.10+
- CMake 3.16+ and a C++17 compiler
- KiCad 8+ / `kicad-cli` for authoritative real-board validation
- Optional OpenMP, OpenCL, Ngspice and OpenEMS/CSXCAD

MIT. HALO-90 is a separate project and is cloned under the gitignored
`third_party/` directory.
