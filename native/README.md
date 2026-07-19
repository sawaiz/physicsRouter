# Native core (`pr_native`)

C++17 **isotropic free-angle** router — the **only** geometric router in physicsRouter (no Python fallback). Two engines:

- **ExactMap** (`exact.cpp`) — clearance authority: rect obstacles in a spatial hash with exact Liang–Barsky segment tests, painted copper with continuous seg–seg distance, and `free_angle_route_exact` (LOS · detours · radar · 1/2/3-corner · hierarchical multi-grid A\* with 16-dir moves · rubberband, congestion-aware).
- **GridMap** (`router.cpp`) — whole-board batch fast path: occupancy grid, any-angle detours, A\*, multi-net MST, post-rubberband, via minimize, multi-site vias with explainable reasons, batch wirelength score, optional OpenCL clearance.

Version: **1.9.1-via-pad-clearance**

## Build

```bash
# from repo root
bash scripts/build_native.sh
# artifacts: native/build/pr_native*.so , native/build/pr_bench
```

Requires: CMake ≥ 3.16, C++17 compiler, Python development headers.  
Optional: OpenMP, OpenCL (Apple M-series GPU works).

## Use from Python

```bash
# native/build is auto-discovered in a dev checkout; PYTHONPATH optional
python -c "from physics_router.native_bridge import info; print(info())"
```

Every `ObstacleMap` query and `free_angle_route` call in Python delegates here. `clearance_aware_route(prefer_native=True)` additionally uses the whole-board GridMap fast path, then Python polish (elastic + SI/MFG).

## Design

| Choice | Rationale |
|--------|-----------|
| Packed `uint8` grid | O(1) cell tests with clearance-correct centerline inflation |
| Atomic full-net commit | Failed multipin trees never leak partial copper |
| Advisory graph tree | Python supplies crossing-aware hypergraph tree edges; blocked edges fall back to any legal frontier connection |
| Conflict-graph layer order | DSATUR colors crossing nets and passes the chosen layer first without bypassing pad reachability |
| Topology-safe rubberband | Two-pin chains shorten; multipin branches remain intact |
| Oriented pad/layer-aware obstacles | Real pad XY, angle, net and copper layers; package bodies do not bury anchors |
| Layer-reachable anchors | SMD pads start/end only on exposed copper; inner escapes use two vias |
| Width-aware obstacle inflation | Physical pad/seed copper is inflated once for the widest bucket track |
| Organic `CopperArea` | Rounded Edge.Cuts-bounded power zones, refilled by KiCad |
| **Isotropic detours** | Perpendicular bulges + angled midpoints before A* (not H/V only) |
| Multi-site vias + `reason` | Explainable layer transitions (mirrors Python UI) |
| Post rubberband + via_minimize | Geometry polish after connectivity |
| Parallel bucket bundles | Stable power/critical/matrix orders run concurrently; best zero-violation completion wins |
| Batch then bounded recovery | Fast legal bucket route; small rejected nets retry individually |
| Sparse PathFinder history | Present and persistent resource costs steer exact/GridMap A* away from repeatedly overused cells |
| Conflict-directed legalization | Exact marker graph selects a maximal legal net set before victim-only repair |
| Copper-edge margin | Track half-width plus the active fabrication edge clearance is reserved around curved Edge.Cuts |
| No via-in-pad | Exact rotated-pad distance rejects physical overlap with every pad; foreign pads additionally receive electrical clearance |
| OpenCL batch clearance | Parallel sample tests after/during validation |
| pybind11 | Zero-copy-friendly lists of segments into Python |

Python still owns the **full** pipeline (K-homotopy, CBS, planner, SI/MFG UI). Native accelerates the geometric core.

## Bench

```bash
./native/build/pr_bench
```

Reports GPU device, route wall time, and score-batch cost.

## Config flags (`RouteConfig`)

| Flag | Default | Meaning |
|------|---------|---------|
| `isotropic` | true | Any-angle detours |
| `post_rubberband` | true | Collapse chains after route |
| `via_minimize` | false | Drop redundant vias only when explicitly requested |
| `atomic_nets` | true | Roll back a net unless all anchors connect |
| `soft_fallback` | false | Never paint illegal copper |
| `use_gpu` | true | OpenCL batch when available |
| `congestion` | empty | Sparse present/historical per-layer resource costs supplied by the board-wide host |
| `edge_clearance_mm` | 0.01 | Copper-to-Edge.Cuts clearance in addition to half track width; Python supplies the active board rule |
