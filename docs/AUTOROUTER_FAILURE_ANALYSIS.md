# Autorouter failure analysis and correction plan

**TL;DR (current):** Committed HALO copper targets **zero hard DRC**. Full multipin completion is still the open stress goal — **open nets beat shorts**. Historical bugs (layer-blind anchors, point vias, wrong outline) are largely fixed; dense CPX remains hard.

| Claim | Status on HALO-class boards |
|-------|------------------------------|
| 1. Geometrically legal committed copper | Goal / largely achieved on committed nets |
| 2. Electrically complete (all nets) | **Not** guaranteed |
| 3. Optimized (length / SI / time) | Secondary until (2) |

For how to *use* the router today: [USER_GUIDE.md](USER_GUIDE.md). For principles: [../DESIGN.md](../DESIGN.md).

This document records what failed on the HALO-90 route, what the released PCB
teaches us, which fixes are implemented, and which work remains.

## Executive diagnosis

The failed job `b9839a3187af` was not primarily a bad A* tuning run. Its model
allowed physically impossible layer connectivity, omitted important fixed
copper, and scored a different object than KiCad checked.

| Failure | Observed evidence | Consequence |
|---|---|---|
| Layer-blind anchors | 68 inner-layer segments, 0 vias, 251/256 SMD pads | Inner copper was counted as connected to front-only pads |
| Point-sized vias | Via sites checked only at their centers | Via annuli and holes overlapped pads and other holes |
| Partial/blind via spans | F.Cu→inner transitions emitted on a board that forbids blind vias | KiCad object did not match the search graph |
| Incomplete pad model | Rotated pads improved, but custom primitives were absent | Coin-cell contact arcs were invisible to search/DRC |
| Weak DRC | Route-to-route only; no route-to-pad check | Cross-net pad shorts scored as clean |
| Wrong outline extent | Line-only Edge.Cuts bbox on an arc board | Unconfigured HALO loaded as 100×80 mm instead of about 24×26 mm |
| Coarse dense prepass | Dense matrix batch forced to a 0.25 mm grid | Nets that routed at 0.15 mm failed in the batch |
| Decorative layer coloring | Same-layer F.Cu was accepted before colored-layer escape | DSATUR assignments did not create vias or distribute copper |
| Greedy order | Sparse nets could occupy the matrix's few annular corridors | Later CPX nets had no legal path |
| Oracle reset | Final native DRC overwrote an already-known KiCad failure count | API reported zero after KiCad reported hundreds |
| Temp-project loss | DRC temp PCB had no matching `.kicad_pro` | KiCad silently substituted generic 0.20 mm rules |
| Blocking call | Timeout checked only between large route calls | A 1,200 s request returned after about 1,288 s |

The correct order of work is therefore: physical model → complete connectivity
→ exact legality → negotiated global completion → secondary optimization.

## Measured HALO evidence

### Board and connectivity

- 111 components, 23 nets and 256 pads.
- 251 SMD pads, four NPTH pads and one plated through-hole pad.
- Four copper layers: `F.Cu`, `In1.Cu`, `In2.Cu`, `B.Cu`.
- Source-project Default rules: about 0.127 mm track/clearance and 0.45/0.20
  mm vias. The default router target applies the more conservative JLCPCB
  4-layer recommendation: 0.15 mm track/clearance and 0.60/0.30 mm vias.
- Ten CPX nets have 19 pins each. Together they dominate routing density.

### Failed monitored job

| Round | Strategy | KiCad copper violations | Segments | Vias | Unrouted |
|---|---:|---:|---:|---:|---:|
| 1 | hybrid | 489 | 192 | 0 | 4 |
| 2 | native | 588 | — | 0 | 4 |
| 3 | native fine | 588 | — | 0 | 4 |

The final API summary then exposed zero violations. This was a reporting bug,
not an improvement.

### Released production copper as a topology reference

The checked-in upstream board has roughly 4,335 track segments, 1,188.5 mm of
track and 182 vias. Its CPX strategy is highly structured:

- CPX-0..2 use F.Cu as their principal long-run layer.
- CPX-3..6 use In1.Cu as their principal long-run layer.
- CPX-7..9 use In2.Cu as their principal long-run layer.
- B.Cu is used mainly for short escapes/crossovers rather than as a primary
  matrix lane.
- Most inner-primary CPX nets have explicit per-pad or per-branch escapes.

That board is electrically complete (`unconnected_count = 0`). Its current
KiCad 10 check is not globally pristine: it includes donor-board text,
courtyard and a small number of fixed copper warnings. It is a topology and
layer-assignment reference, not a zero-DRC geometry oracle to copy blindly.

### Corrected v1.9.1 checkpoint

The current deterministic hybrid run commits 12/23 HALO nets, including
CPX-1 and CPX-2, as 941 segments and 271.8 mm total track. Eleven nets remain
atomically open. It currently selects no vias or areas.
The generated copper has:

- zero native track/track, track/pad, via/pad and outline hard violations;
- zero route-attributed KiCad copper errors with the source project loaded;
- no partial copper for rejected nets;
- 168 KiCad unconnected items because the board route is incomplete.

The v1.9 snapshot had appeared clean only because same-net via/pad contacts
were exempted from the router audit. Rechecking it with the v1.9.1 invariant
found 33 via/pad violations among its 42 vias. Search now measures every via
annulus against exact rotated pad rectangles on every traversed copper layer:
same-net pads forbid physical overlap, while foreign pads also require normal
electrical clearance. The replacement artifact removes all 33 violations.
Its zero-via result is a real routing-quality regression, not a reporting bug:
the current 0.60/0.30 mm via rule cannot yet find enough offset fanout sites in
the dense 0402 ring, so the legalizer retains outer-layer nets instead.

The board-wide negotiation pass generated 21/23 complete temporary candidates
in its first round. Those candidates were deliberately not legal output: exact
DRC still found more than 2,000 conflict markers. Across three rounds the
active conflict set changed from 23 to 17 to 14 nets and coarse overused cells
fell from 2,825 to 1,163. Exact conflict-graph legalization plus targeted
repair then retained 12 legal nets. This is a one-net improvement over v1.8,
but the unresolved CPX bundle and the 941-segment geometry show that
negotiation is now infrastructure, not proof of full convergence.

This is much more honest than the failed run and proves the corrected geometry
path, but it is not a successful full-board result.

## Implemented corrections

### 1. Hypergraph and annular topology

Every multi-pin net is a hyperedge. Candidate trees are crossing-aware, and a
weighted conflict graph is colored with deterministic DSATUR. For annular
placements, the planner now:

1. finds radial pad bands from the whole placement center;
2. sorts each band by polar angle;
3. opens the chain at its most expensive/congested angular gap;
4. joins adjacent bands by their least-cost bridge;
5. connects central pins without a high-degree spoke fanout.

On the full HALO graph this lowers projected guide length from about 820 mm to
about 790 mm and recognizes all ten CPX nets as annular candidates. The metric
is a guide objective, not a promise that every guide can be geometrized.

### 2. Physical pin access and atomic connectivity

Pad anchors carry their real exposed copper layers. A front SMD pad cannot
collect an inner segment. A net commits only if every anchor is in one
multilayer connected component; failed attempts leave no stubs.

### 3. Legal via objects

- Diameter/drill come from the active rules.
- With blind/buried vias disabled, every transition emits an F.Cu–B.Cu through
  via and checks every traversed layer.
- Search checks the via disk, not only its center.
- Exact rotated-pad distance forbids via-in-pad on owning nets and applies full
  clearance to foreign pads on every traversed copper layer.
- A separate hole occupancy map enforces same-net hole spacing without
  incorrectly blocking legal same-net track overlap.
- Inner-colored SMD trees attempt explicit two-via escape/fanout before an
  outer-layer fallback.

### 4. Complete fixed-copper model

Native search and Python DRC use oriented, layer-specific, net-owned pads.
No-net copper blocks every route. KiCad custom-pad strokes are sampled and
painted as oriented obstacles; this is required for HALO's large battery-holder
contact arcs, whose copper extends far beyond the pad anchor `size`.

### 5. Matching DRC and serialized metrics

Fast DRC now checks route/route, route/pad, via/pad, hole spacing and outline
samples. KiCad remains the final fill and rule oracle. Temporary validation
copies the source `.kicad_pro`/`.kicad_dru`, and route attribution excludes
pre-existing fixed-board violations. The KiCad count is preserved in the final
route object and API response instead of being reset by a later native pass.

### 6. Dense-board search policy

- Dense batches use a 0.15 mm grid instead of the old 0.25 mm floor.
- Power/plane resources are reserved first, constrained critical nets second,
  and the dense matrix is then rebuilt as a whole against that fixed seed.
- Six deterministic matrix orders are evaluated.
- Rejected dense nets receive one atomic single-net retry against the legal
  committed subset.
- Open copper remains preferable to a short or false connection.

## What established routers do differently

| Router/research scheme | Relevant mechanism | physicsRouter status |
|---|---|---|
| PathFinder negotiated congestion | Temporarily permits sharing, then raises historical resource costs until conflicts disappear | Implemented board-wide with three bounded rounds and sparse historical cell costs; HALO conflicts reduce but do not converge yet |
| TritonRoute | Explicit pin-access reservation, local workers, marker-driven rip-up, complete-or-unrouted net output | Pin access, exact-marker conflict graph, victim-only repair and atomic output implemented; local detailed repair remains limited |
| Rubber-band layer assignment | Fix topological path classes before geometry; split trees into sections and add vias at layer crossings | Per-tree-edge layer assignment and access-via feasibility are implemented; shared branch escapes and true corridor detours remain |
| Conflict-Based Search / MLV-CBS | Branch on route conflicts instead of relying only on route order; combine with congestion negotiation | Deterministic maximal-independent conflict legalization is in the primary flow; bounded branching over alternate route candidates remains experimental |
| TopoR | Free-angle, topology-preserving geometry and automatic optimization | Free-angle, rubber-band and global conflict negotiation implemented; dense topology refinement and safe consolidation remain incomplete |
| FreeRouting | Rip-up/reroute, stagnation detection, partial output, DRC feedback | Order variants and partial output implemented; stagnation hashes/history costs remain |

The key distinction is that mature routers do not make one irreversible greedy
pass. They separate topology from geometry, reserve pin access, and negotiate
scarce resources across nets.

## Scientific basis

- Dayan and Dai's **Layer Assignment for Rubber Band Routing** describes
  topology-before-geometry, Steiner-tree section assignment, explicit vias at
  layer crossings and cost balancing between length and vias:
  [UCSC-CRL-93-04](https://tr.soe.ucsc.edu/sites/default/files/technical-reports/UCSC-CRL-93-04.pdf).
- McMurchie and Ebeling's **PathFinder** introduces negotiated congestion and
  historical resource costs rather than permanent greedy ownership:
  [PathFinder paper](https://janders.eecg.utoronto.ca/1387/readings/pathfinder.pdf).
- Kahng et al.'s **TritonRoute** details pin-access analysis, local detailed
  routing workers, DRC markers and rip-up of marked nets:
  [TritonRoute paper](https://vlsicad.ucsd.edu/Publications/Journals/j133.pdf).
- The multilayer PCB **MLV-CBS** work applies conflict-based search,
  line-to-line routing, adaptive heatmaps and negotiated order to reduce vias
  and resolve dense conflicts:
  [Integration, the VLSI Journal article](https://www.sciencedirect.com/science/article/pii/S0167926025001907).
- OpenROAD documents the production TritonRoute flow and its pin-access,
  search-and-repair and DRC stages:
  [OpenROAD detailed routing documentation](https://openroad.readthedocs.io/en/latest/main/src/drt/README.html).
- KiCad's shove/router documentation is the reference for interactive
  clearance-preserving routing behavior:
  [KiCad PCB Editor manual](https://docs.kicad.org/master/en/pcbnew/pcbnew.html).
- FreeRouting's official releases document DRC checks, corrected layer
  handling, stagnation detection and partial-route behavior:
  [FreeRouting releases](https://github.com/freerouting/freerouting/releases).
- TopoR's official material describes its free-angle/topological optimization
  model:
  [TopoR product documentation](https://www.eremex.com/products/topor/).

Graph theory is directly useful: hypergraphs, spanning trees, conflict graphs,
coloring, articulation points and connected components all map to PCB routing
decisions. Classical knot invariants are much less useful here because PCB
traces are open arcs on several layers rather than closed knots in continuous
3-space. Homotopy classes and planar embeddings are the more appropriate
topological tools.

## Highest-impact remaining work

1. **Conflict-component route alternatives:** create multiple homotopy and
   layer candidates for each exact marker component, then branch on the
   conflicting pair instead of rerunning one deterministic path. Stop on
   stagnation hashes or a hard deadline.
2. **Pin-access resource sharing:** the exact access oracle now precomputes and
   reserves legal rule-size offset vias. The next step is to model one escape as
   a reusable branch resource instead of charging or inserting it independently
   for every incident tree edge.
3. **Corridor global routing:** the section planner now assigns each topology
   edge a layer with PathFinder-style coarse capacity. It must next search
   alternate coarse paths around obstacles rather than pricing only the direct
   section cells.
4. **Topology-preserving consolidation:** the current legal HALO artifact has
   941 segments. Collapse collinear/A* chains only when the embedded route
   graph and exact DRC remain unchanged.
5. **Power topology:** connect narrow pad escapes to native/KiCad-filled GND
   and +3V areas, with thermals/refill included in connectivity validation.
6. **Hard native deadline/cancellation:** propagate a monotonic deadline into
   C++ search loops; a released GIL makes the UI responsive but does not stop a
   long native attempt by itself.
7. **Provenance:** embed source commit/compiler/build flags in the native
   module and every route artifact to make stale binaries impossible to miss.

Implemented in the v2 production-flow rewrite: exact pin-access preflight,
per-section layer assignments, detailed-router consumption of reserved via
sites, and an explicit all-nets-plus-zero-DRC manufacturing gate. These remove
false success modes; they do not retroactively turn the recorded 12/23 HALO
artifact into a completed route.

On HALO, the preflight also exposed a rule-profile feasibility constraint:
0.60/0.30 mm vias make only 122/240 SMD anchors inner-reachable, while the
source-board-compatible JLCPCB capability profile (0.45/0.20 mm, 0.125 mm
radial annulus) reaches 191/240. The remaining 49 anchors are deliberately
outer-only; the global planner cannot spend an impossible via there.

The acceptance gate remains: 23/23 nets, zero route-attributed KiCad copper
errors after refill, no missing/illegal vias, and identical metrics in the
artifact, viewer and API.
