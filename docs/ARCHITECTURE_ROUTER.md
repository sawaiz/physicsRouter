# Topology-first router architecture

How **physicsRouter** implements a TopoR-**philosophy** router without copying Eremex binaries: topological connectivity, delayed geometry, multi-variant optimization, continuous reshaping, and negotiated conflict resolution.

Companion docs: [TOPOR.md](TOPOR.md) (commercial product model), [RESEARCH.md](../RESEARCH.md) (bibliography).

---

## Design thesis

> **Find and optimize flexible topological relationships first, then repeatedly derive exact geometry.**  
> Failures during geometrization must feed **back into topology**, not trap the search in a bad global corridor.

That thesis appears across:

| Source | Contribution |
|--------|----------------|
| Eremex TopoR (public manuals/papers) | Isotropic free-angle, multi-variant, elastic geometry, via remove, component shift |
| Dayan 1997 rubberband / Blake toporouter | Independent inspectable topology→geometry lineage |
| FreeRouting (GPL-3 study only) | Specctra I/O, maze/insert/batch patterns — **study, not copy** for non-GPL products |
| US 7,937,681 / US 2006/0242614 | Topology graph → relaxed constraints → tighten → geometry → feedback |
| US 7,017,137 | Topology ignoring width/clearance → guide points → detail |
| US 8,510,703 | Routing-space representation; **via placement as global planning** |
| US 2023/0306177 | Curvilinear any-direction; hybrid free-angle + preferred layers |
| 3D LineExplore (Sci. Reports 2026) | Continuous-space radar exploration, interlayer transitions |
| CBS / MAPF PCB papers (~2025) | Conflict branches on net pairs |
| Negotiated congestion (FPGA routers) | Overuse then raise historical resource cost |
| NS-Place (arXiv:2210.14259) | Placement that leaves routable separation |
| PCBWorld / PCB-Dreamer (2026) | Engine-grounded RL manager, not pixel copper nets |

**Patent note:** published patents inform design discussion; they are **not** a license to practice claims. Freedom-to-operate review before commercial ship.

---

## Three simultaneous representations

physicsRouter keeps these levels conceptually separate (module `physics_router.topology`):

```text
┌─────────────────────────────────────────────────────────────┐
│ 1. TOPOLOGICAL                                              │
│    TopologySignature: net · layer · obstacle L/R sides ·    │
│    via count · layers used                                   │
│    Survives small component/via moves (homotopy class)       │
└───────────────────────────┬─────────────────────────────────┘
                            │ choose class
┌───────────────────────────▼─────────────────────────────────┐
│ 2. SPARSE GEOMETRIC GRAPH                                   │
│    radar_scan_points · obstacle corners · portals · pads    │
│    CongestionMap cells (present + historical costs)         │
└───────────────────────────┬─────────────────────────────────┘
                            │ geometrize
┌───────────────────────────▼─────────────────────────────────┐
│ 3. EXACT GEOMETRY                                           │
│    RouteSegment polylines · Via · CopperArea · DRC widths   │
│    (future: filled-zone polygons / biarcs / pair offsets)   │
└─────────────────────────────────────────────────────────────┘
```

### Clearance expansion

Obstacles are expanded by track half-width + clearance (Minkowski-style via `ObstacleMap` inflate/paint). Pads retain net ownership and copper-layer membership, so their own net can reach them while foreign copper is excluded. The centerline only avoids **expanded** obstacles.

---

## Main pipeline

```text
Classify nets (power / critical / matrix / general)
            │
            ▼
Reserve rounded power/ground CopperArea polygons on plane-preferred layers
            │
            ▼
For each remaining bucket:
    Native GridMap atomic batch
        · exact pad XY / net ownership / copper layers
        · Edge.Cuts occupancy + clearance-correct inflation
        · LOS → isotropic detours → any-angle A*
        · full multipin tree commits only when every anchor connects
    Power/critical/matrix buckets: bounded stable orders run concurrently
    Native exact DRC gate
    Select most-complete legal variant; bounded recovery for small rejects
            │
            ▼
Board-wide negotiated congestion:
    · route independent candidates with soft present sharing
    · raise historical cost on overused cells and exact DRC markers
    · reroute only the exact conflict component
    · legalize maximal independent conflict sets
    · retry only removed/incomplete victims
            │
            ▼
Post-connect re-geometry only if exact DRC does not worsen
            │
            ▼
Report legal completion · preserve rejected nets as explicitly open
```

| Phase | Code |
|-------|------|
| Native geometric core | `native/router.cpp`, exposed by `native_bridge.py` |
| Atomic batch orchestration | `router._route_native_batch` / `hybrid_route.py` |
| Parallel bucket bundles | `hybrid_route._matrix_order_variants` + native GIL-free workers |
| Pad/layer-aware obstacles | `router._native_obstacle_map` |
| Organic copper areas | Native `CopperArea` → `RouteResult.areas` → KiCad zones |
| Isotropic free-angle | `router.free_angle_route` (`style=isotropic`) |
| Radar / sparse candidates | `topology.radar_scan_points` |
| Negotiated congestion | `topology.CongestionMap` |
| Board-wide PathFinder host | `negotiated_congestion.negotiated_congestion_route` |
| Multi-variant + Pareto | `routing_strategies.topor_style_route` (optional outer strategy) |
| Elastic geometry | `router.rubberband_cleanup` |
| Post-connect re-geometry | `regeometry.post_connect_regeometry` (subdivide, spacing, arcs) |
| Via minimize | `router.remove_redundant_vias` |
| Topology explainability | `topology.signatures_from_route` |

---

## Cost model (negotiated)

\[
C = C_{\mathrm{length}} + C_{\mathrm{via}} + C_{\mathrm{present}} + C_{\mathrm{historical}}
\]

Optional future terms (not all implemented): SI, manufacturing risk, bend, return-path.

Persistent conflict regions accumulate **historical** cost so nets move into alternate homotopy classes — TopoR’s “route first, resolve later” in modern form.

---

## What we deliberately do **not** do

1. **End-to-end neural nets drawing copper** — illegal geometry risk; use ML only for high-level choices later (order, rip-up set, homotopy pick).
2. **Soft illegal straight copper** in clearance mode — open edges beat fake completion ([DESIGN.md](../DESIGN.md)).
3. **GPL FreeRouting code in the product** — DSN/SES interchange + independent implementation; study FR for algorithms only.
4. **Full CBS on thousands of nets** — reserve conflict-cluster CBS / CP-SAT for dense local regions (roadmap).

---

## Roadmap vs this thesis

| Phase | Status |
|-------|--------|
| KiCad/DSN import, obstacle expansion, free-angle single-net | **Done** |
| Rubberband + via remove + multi-variant | **Done** |
| Topology signatures + radar scan + negotiated congestion | **Done** |
| K distinct homotopy alternatives per net (dedup by signature) | **Done** (`homotopy.py`) |
| Conflict-cluster CBS + optional CP-SAT vias | **Done** (`conflict_cbs.py`) |
| Continuous elastic optimization (forces) | **Done** (`elastic.py`) |
| Post-connect free-angle re-geometry (spacing + multi-bend + arcs) | **Done** (`regeometry.py`); metrics in `quality.topor_geometry` |
| Learned-style high-level planner (feature linear policy) | **Done** (`planner.py`) |
| SI + manufacturing score terms | **Done** (`si_mfg.py`) |
| Explainable “why this via” UI | **Done** (via.reason + Route panel) |
| Atomic native multipin transactions | **Done** (`RouteConfig.atomic_nets`) |
| Oriented pad/net/layer-aware native obstacles | **Done** |
| SMD anchor layer reachability + two-via escape | **Done** |
| Native organic power/ground areas | **Done** (refillable KiCad zones) |
| Bounded bucket rebuild | **Done** (parallel power/critical/matrix variants) |
| Multi-pin net hypergraph | **Done** (`graph_theory.py`; one hyperedge per net) |
| Crossing-aware spanning topology | **Done** (weighted Kruskal tree, native advisory edge order) |
| Net conflict graph + layer coloring | **Done** (weighted deterministic DSATUR) |
| Embedded route graph audit | **Done** (components, cycles, crossings, articulation points, bridges) |
| Board-wide PathFinder history + exact conflict rip-up | **Done** (`negotiated_congestion.py`; bounded, monotonic legal output) |
| Conflict-component homotopy branching | Roadmap — main HALO-90 completion blocker |
| Incremental invalidation on component move | Roadmap |
| End-to-end RL manager / PCBWorld | Roadmap |

### Strongest combined design (target)

> **TopoR-style topology + elastic geometry + continuous-space exploration + negotiated congestion + local exact conflict solving + learned high-level planning + electrical/manufacturing costs.**

---

## Evaluation

- Synthetic + HALO-90 boards (`pytest`, `scripts/ci_regression.py`)
- Compare length / vias / unrouted / grade vs guide and FreeRouting SES when available
- Optional: reproduce Eremex sample boards from [TopoR 6.0 examples](https://www.eremex.com/support/tutorials/topor6_0_examples/) as behavioral tests (completion, vias, topology)

The checked-in v1.9 HALO-90 snapshot commits 12/23 nets (including CPX-0,
CPX-1, 42 explicit vias, and one GND area), leaves 11 nets explicitly open,
and reports zero native track/via shorts, spacing hits, or Edge.Cuts escapes.
Its topology plan contains 240 vertices, 23 hyperedges, 217 tree edges, and
77 net-conflict edges; the emitted route has zero same-layer crossings.
This replaces the invalid 17/23 result that treated inner-layer copper as
connected directly to front-only SMD pads. Native area DRC covers the zone
boundary; KiCad refill and DRC remain the fabrication authority for the
filled polygon and thermals. See
[`drc_report.json`](images/routing_process/drc_report.json).

---

## Module map

| Path | Role |
|------|------|
| `src/physics_router/graph_theory.py` | Board hypergraph, crossing-aware Kruskal, DSATUR layer coloring, route graph audits |
| `src/physics_router/topology.py` | Signatures, radar, congestion, Pareto scores |
| `src/physics_router/negotiated_congestion.py` | Board-wide history, resource ownership, exact conflict graph, victim repair |
| `src/physics_router/router.py` | Graph guide, free-angle policy, rubberband, vias, apply-to-KiCad |
| `native/router.cpp` | Advisory tree geometrization, atomic GridMap batch router, native copper areas |
| `src/physics_router/regeometry.py` | Post-connect free-angle reshape + TopoR geometry metrics |
| `scripts/render_routing_process.py` | Doc renders: placement → guide → clearance → re-geometry strip |
| `src/physics_router/routing_strategies.py` | `topor_style_route` orchestration |
| `src/physics_router/dsn_export.py` | Specctra DSN for external baselines |
| `docs/TOPOR.md` | Commercial TopoR product reference + images |

Process figures live under [`docs/images/routing_process/`](images/routing_process/) (see that folder’s README).
