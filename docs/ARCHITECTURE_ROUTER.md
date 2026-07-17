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
│    RouteSegment polylines · Via · rubberband · DRC widths   │
│    (future: arcs / biarcs / pair offsets)                   │
└─────────────────────────────────────────────────────────────┘
```

### Clearance expansion

Obstacles are expanded by track half-width + clearance (Minkowski-style via `ObstacleMap` inflate/paint). The centerline only avoids **expanded** obstacles.

---

## Main pipeline (`topor_style_route`)

```text
Generate net-order variants (priority / small_first / large_first / via_averse)
            │
            ▼
For negotiate_iter in 1..N:
    For each variant:
        clearance_aware_route (isotropic free-angle, soft_fallback=OFF)
            · LOS → isotropic detours + radar portals → congestion-aware A*
            · optional vias (global layer plan, not emergency only)
        rubberband_cleanup (topology fixed, geometry shortens)
        remove_redundant_vias
        paint CongestionMap.present
    CongestionMap.negotiate()  # historical cost ↑ on crowded cells
            │
            ▼
Pareto front of ScoreVectors (unrouted, vias, length, congestion, …)
            │
            ▼
Pick winner · record topology_signatures · DRC widths
```

| Phase | Code |
|-------|------|
| Isotropic free-angle | `router.free_angle_route` (`style=isotropic`) |
| Radar / sparse candidates | `topology.radar_scan_points` |
| Negotiated congestion | `topology.CongestionMap` |
| Multi-variant + Pareto | `routing_strategies.topor_style_route` |
| Elastic geometry | `router.rubberband_cleanup` |
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
| Topology signatures + radar scan + negotiated congestion | **Done (v1)** |
| K distinct homotopy alternatives per net (dedup by signature) | Partial |
| Rip-up/reroute conflict clusters | Roadmap |
| Continuous elastic optimization (forces) | Roadmap |
| Local CP-SAT / CBS on BGA/connector | Roadmap |
| Learned net order / routability | Roadmap |
| Physics/manufacturing score terms | Partial (placement scores) |
| Incremental invalidation on component move | Roadmap |
| Explainable “why this via” UI | Signatures stored; UI next |

### Strongest combined design (target)

> **TopoR-style topology + elastic geometry + continuous-space exploration + negotiated congestion + local exact conflict solving + learned high-level planning + electrical/manufacturing costs.**

---

## Evaluation

- Synthetic + HALO-90 boards (`pytest`, `scripts/ci_regression.py`)
- Compare length / vias / unrouted / grade vs guide and FreeRouting SES when available
- Optional: reproduce Eremex sample boards from [TopoR 6.0 examples](https://www.eremex.com/support/tutorials/topor6_0_examples/) as behavioral tests (completion, vias, topology)

---

## Module map

| Path | Role |
|------|------|
| `src/physics_router/topology.py` | Signatures, radar, congestion, Pareto scores |
| `src/physics_router/router.py` | Free-angle search, rubberband, vias, apply-to-KiCad |
| `src/physics_router/routing_strategies.py` | `topor_style_route` orchestration |
| `src/physics_router/dsn_export.py` | Specctra DSN for external baselines |
| `docs/TOPOR.md` | Commercial TopoR product reference + images |
