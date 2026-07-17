cmake_minimum_required(VERSION 3.16)
project(physics_router_native LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(PR_WITH_OPENMP "Enable OpenMP multi-threading" ON)
option(PR_WITH_OPENCL "Enable OpenCL GPU batch clearance" ON)
option(PR_BUILD_PYTHON "Build pybind11 Python module" ON)
option(PR_BUILD_CLI "Build native CLI benchmark" ON)

find_package(Threads REQUIRED)

add_library(pr_core STATIC
  src/grid.cpp
  src/router.cpp
  src/score.cpp
  src/gpu_opencl.cpp
)
target_include_directories(pr_core PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/include
)
target_link_libraries(pr_core PUBLIC Threads::Threads)

if(PR_WITH_OPENMP)
  find_package(OpenMP)
  if(OpenMP_CXX_FOUND)
    target_link_libraries(pr_core PUBLIC OpenMP::OpenMP_CXX)
    target_compile_definitions(pr_core PUBLIC PR_HAS_OPENMP=1)
    message(STATUS "OpenMP enabled")
  else()
    message(STATUS "OpenMP not found — CPU serial for parallel regions")
  endif()
endif()

if(PR_WITH_OPENCL)
  find_package(OpenCL QUIET)
  if(OpenCL_FOUND)
    target_link_libraries(pr_core PUBLIC OpenCL::OpenCL)
    target_compile_definitions(pr_core PUBLIC PR_HAS_OPENCL=1)
    # Embed kernel path relative to source
    target_compile_definitions(pr_core PRIVATE
      PR_OPENCL_KERNEL_DIR="${CMAKE_CURRENT_SOURCE_DIR}/kernels")
    message(STATUS "OpenCL GPU enabled")
  else()
    message(STATUS "OpenCL not found — GPU path disabled")
  endif()
endif()

# Aggressive but portable optimisations
if(NOT MSVC)
  target_compile_options(pr_core PRIVATE -O3 -ffast-math)
  if(CMAKE_SYSTEM_PROCESSOR MATCHES "arm64|aarch64")
    # Apple Silicon: keep portable; compiler auto-vectorizes
  elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|AMD64")
    target_compile_options(pr_core PRIVATE -march=native)
  endif()
else()
  target_compile_options(pr_core PRIVATE /O2)
endif()

if(PR_BUILD_CLI)
  add_executable(pr_bench src/bench_main.cpp)
  target_link_libraries(pr_bench PRIVATE pr_core)
endif()

if(PR_BUILD_PYTHON)
  find_package(Python3 COMPONENTS Interpreter Development REQUIRED)
  # Fetch pybind11 if not installed
  include(FetchContent)
  FetchContent_Declare(
    pybind11
    GIT_REPOSITORY https://github.com/pybind/pybind11.git
    GIT_TAG v2.13.6
  )
  FetchContent_MakeAvailable(pybind11)
  pybind11_add_module(pr_native src/bindings.cpp)
  target_link_libraries(pr_native PRIVATE pr_core)
  # Install into package
  install(TARGETS pr_native LIBRARY DESTINATION .)
endif()
