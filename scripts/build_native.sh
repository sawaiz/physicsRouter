#!/usr/bin/env bash
# Build C++/OpenMP/OpenCL native core + Python module
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BUILD_DIR="${BUILD_DIR:-native/build}"
cmake -S native -B "$BUILD_DIR" \
  -DCMAKE_BUILD_TYPE=Release \
  -DPR_WITH_OPENMP=ON \
  -DPR_WITH_OPENCL=ON \
  -DPR_BUILD_PYTHON=ON \
  -DPR_BUILD_CLI=ON
cmake --build "$BUILD_DIR" -j"$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)"

# Expose module on PYTHONPATH for in-tree use
echo "Built artifacts in $BUILD_DIR"
ls -la "$BUILD_DIR"/pr_native* "$BUILD_DIR"/pr_bench 2>/dev/null || true
echo "Run: PYTHONPATH=$BUILD_DIR:src python -c 'from physics_router.native_bridge import info; print(info())'"
echo "Bench: $BUILD_DIR/pr_bench"
