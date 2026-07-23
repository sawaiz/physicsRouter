#!/usr/bin/env bash
# Build C++/OpenMP/OpenCL native core + Python module
# Maximizes host utilization: OpenMP on all cores + OpenCL GPU when present.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BUILD_DIR="${BUILD_DIR:-native/build}"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)"

# Homebrew libomp (Apple clang does not ship OpenMP)
CMAKE_EXTRA=()
if [[ "$(uname -s)" == "Darwin" ]]; then
  for OMP_ROOT in /opt/homebrew/opt/libomp /usr/local/opt/libomp; do
    if [[ -d "$OMP_ROOT" ]]; then
      CMAKE_EXTRA+=(
        -DOpenMP_C_FLAGS="-Xpreprocessor -fopenmp -I${OMP_ROOT}/include"
        -DOpenMP_CXX_FLAGS="-Xpreprocessor -fopenmp -I${OMP_ROOT}/include"
        -DOpenMP_C_LIB_NAMES=omp
        -DOpenMP_CXX_LIB_NAMES=omp
        -DOpenMP_omp_LIBRARY="${OMP_ROOT}/lib/libomp.dylib"
      )
      export DYLD_LIBRARY_PATH="${OMP_ROOT}/lib:${DYLD_LIBRARY_PATH:-}"
      break
    fi
  done
fi

# Use all cores during search unless user overrides
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-$JOBS}"
export OMP_PROC_BIND="${OMP_PROC_BIND:-close}"
export OMP_PLACES="${OMP_PLACES:-cores}"

cmake -S native -B "$BUILD_DIR" \
  -DCMAKE_BUILD_TYPE=Release \
  -DPR_WITH_OPENMP=ON \
  -DPR_WITH_OPENCL=ON \
  -DPR_BUILD_PYTHON=ON \
  -DPR_BUILD_CLI=ON \
  "${CMAKE_EXTRA[@]}"
cmake --build "$BUILD_DIR" -j"$JOBS"

# Expose module on PYTHONPATH for in-tree use
echo "Built artifacts in $BUILD_DIR (OMP_NUM_THREADS=$OMP_NUM_THREADS)"
ls -la "$BUILD_DIR"/pr_native* "$BUILD_DIR"/pr_bench 2>/dev/null || true
echo "Run: PYTHONPATH=$BUILD_DIR:src python -c 'from physics_router.native_bridge import info; print(info())'"
echo "Bench: $BUILD_DIR/pr_bench"
