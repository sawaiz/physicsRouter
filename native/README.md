# Native core (`pr_native`)

**TL;DR:** C++17 is the **only** geometric router. Python never falls back to a pure-Python maze. Build with `bash scripts/build_native.sh`, then `from physics_router.native_bridge import info`.

| Piece | File | Role |
|-------|------|------|
| ExactMap | `exact.cpp` | Clearance authority + free-angle path |
| GridMap / board | `router.cpp` | Batch multi-net + polish |
| Capacity mesh | `capacity_mesh.cpp` | Hierarchical plan before detail |
| Bindings | `bindings.cpp` | pybind11 → Python |

Version: **2.0.0-production-flow**

---

## Build

```bash
# from repo root
bash scripts/build_native.sh
# → native/build/pr_native*.so , native/build/pr_bench
```

Requires: CMake ≥ 3.16, C++17, Python headers. Optional: OpenMP, OpenCL (Apple M-series works).

```bash
python -c "from physics_router.native_bridge import info; print(info())"
./native/build/pr_bench
```

---

## Engines

**ExactMap** — rect obstacles in a spatial hash, painted copper with continuous segment distance, `free_angle_route_exact` (LOS → detours → radar → corner chains → multi-grid A\* → rubberband).

**GridMap** — whole-board occupancy, any-angle detours, multi-net MST, via minimize, multi-site vias with reasons, optional OpenCL batch clearance.

**Capacity mesh** — hierarchical cells + section layer plan *before* free-angle detail. Does not paint copper. See [../docs/CAPACITY_MESH.md](../docs/CAPACITY_MESH.md).

---

## Design choices (scan table)

| Choice | Why |
|--------|-----|
| Atomic full-net commit | No multipin stub copper |
| Exact pin-access sites | Via-in-pad forbidden; finite escapes |
| Capacity mesh default on | Global layer / congestion plan first |
| Isotropic detours | TopoR-style any-angle, not H/V only |
| Soft fallback off | Open > short |
| Topology-safe rubberband | Multipin trees stay connected |
| Width-aware inflation | Obstacle keepouts match track width |
| OpenCL batch | Parallel clearance samples when available |

Python still owns: K-homotopy variants, CBS, planner policy, SI/MFG UI, KiCad write-back.

---

## `RouteConfig` flags

| Flag | Default | Meaning |
|------|---------|---------|
| `isotropic` | true | Any-angle detours |
| `post_rubberband` | true | Collapse chains after route |
| `via_minimize` | false | Drop redundant vias only when requested |
| `atomic_nets` | true | Roll back unless all anchors connect |
| `soft_fallback` | false | Never paint illegal copper |
| `use_gpu` | true | OpenCL when available |
| `enable_capacity_mesh` | true | Hierarchical plan before detail |
| `capacity_effort` | ~0.55 | Mesh depth / refinement 0..1 |
| `edge_clearance_mm` | from rules | Copper-to-Edge.Cuts margin |

---

## Doc map

- Product docs: [../docs/README.md](../docs/README.md)  
- Design: [../DESIGN.md](../DESIGN.md)  
- Capacity mesh: [../docs/CAPACITY_MESH.md](../docs/CAPACITY_MESH.md)
