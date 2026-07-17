# Native core (`pr_native`)

C++17 implementation of the routing hot path: occupancy **grid**, **A\***, multi-net **MST** growth, batch **wirelength** score, optional **OpenCL** segment clearance.

## Build

```bash
# from repo root
bash scripts/build_native.sh
# artifacts: native/build/pr_native*.so , native/build/pr_bench
```

Requires: CMake ≥ 3.16, C++17 compiler, Python development headers.  
Optional: OpenMP, OpenCL.

## Use from Python

```bash
export PYTHONPATH=native/build:src
python -c "from physics_router.native_bridge import info; print(info())"
```

`clearance_aware_route()` calls the native backend when the module imports successfully.

## Design

| Choice | Rationale |
|--------|-----------|
| Packed `uint8` grid | O(1) cell tests vs O(n) rect lists |
| Sequential net order | Paint order must stay deterministic for clearance |
| OpenCL batch clearance | Parallel sample tests after/during validation |
| pybind11 | Zero-copy-friendly lists of segments into Python |

## Bench

```bash
./native/build/pr_bench
```

Reports GPU device, route wall time, and score-batch cost.
