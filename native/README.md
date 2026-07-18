# Native core (`pr_native`)

C++17 **isotropic free-angle** router (TopoR-style hot path): occupancy **grid**, any-angle detours, 8-dir **A\***, multi-net **MST**, post-**rubberband**, **via minimize**, multi-site vias with **explainable reasons**, batch **wirelength** score, optional **OpenCL** clearance.

Version: **1.1.0-native-isotropic**

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
export PYTHONPATH=native/build:src
python -c "from physics_router.native_bridge import info; print(info())"
```

`clearance_aware_route(prefer_native=True)` uses native isotropic route, then optional Python polish (elastic + SI/MFG).

## Design

| Choice | Rationale |
|--------|-----------|
| Packed `uint8` grid | O(1) cell tests vs O(n) rect lists |
| **Isotropic detours** | Perpendicular bulges + angled midpoints before A* (not H/V only) |
| Multi-site vias + `reason` | Explainable layer transitions (mirrors Python UI) |
| Post rubberband + via_minimize | Geometry polish after connectivity |
| Sequential net order | Paint order must stay deterministic for clearance |
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
| `via_minimize` | true | Drop redundant vias |
| `soft_fallback` | false | Never paint illegal copper |
| `use_gpu` | true | OpenCL batch when available |
