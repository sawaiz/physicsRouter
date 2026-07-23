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
  src/exact.cpp
  src/score.cpp
  src/gpu_opencl.cpp
  src/capacity_mesh.cpp
)
target_include_directories(pr_core PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/include
)
target_link_libraries(pr_core PUBLIC Threads::Threads)

if(PR_WITH_OPENMP)
  # Apple Silicon / Homebrew: find libomp explicitly when CMake's default fails
  if(APPLE AND NOT OpenMP_CXX_FOUND)
    foreach(_omp_root "/opt/homebrew/opt/libomp" "/usr/local/opt/libomp")
      if(EXISTS "${_omp_root}/lib/libomp.dylib" OR EXISTS "${_omp_root}/lib/libomp.a")
        set(OpenMP_C_FLAGS "-Xpreprocessor -fopenmp -I${_omp_root}/include")
        set(OpenMP_CXX_FLAGS "-Xpreprocessor -fopenmp -I${_omp_root}/include")
        set(OpenMP_C_LIB_NAMES "omp")
        set(OpenMP_CXX_LIB_NAMES "omp")
        set(OpenMP_omp_LIBRARY "${_omp_root}/lib/libomp.dylib")
        if(NOT EXISTS "${OpenMP_omp_LIBRARY}")
          set(OpenMP_omp_LIBRARY "${_omp_root}/lib/libomp.a")
        endif()
        list(APPEND CMAKE_PREFIX_PATH "${_omp_root}")
        break()
      endif()
    endforeach()
  endif()
  find_package(OpenMP)
  if(OpenMP_CXX_FOUND)
    target_link_libraries(pr_core PUBLIC OpenMP::OpenMP_CXX)
    # MinGW/GCC needs libgomp explicitly for GOMP_* symbols
    if(MINGW OR (CMAKE_CXX_COMPILER_ID STREQUAL "GNU" AND NOT APPLE))
      target_link_libraries(pr_core PUBLIC gomp)
    endif()
    target_compile_definitions(pr_core PUBLIC PR_HAS_OPENMP=1)
    message(STATUS "OpenMP enabled (${OpenMP_CXX_FLAGS})")
  else()
    message(STATUS "OpenMP not found — CPU serial for parallel regions")
  endif()
endif()

if(PR_WITH_OPENCL)
  find_package(OpenCL QUIET)

  # Khronos headers when CUDA/SDK is missing (driver still ships OpenCL.dll)
  if(NOT OpenCL_FOUND OR NOT OpenCL_INCLUDE_DIR)
    include(FetchContent)
    FetchContent_Declare(
      OpenCLHeaders
      GIT_REPOSITORY https://github.com/KhronosGroup/OpenCL-Headers.git
      GIT_TAG v2024.10.24
    )
    FetchContent_GetProperties(OpenCLHeaders)
    if(NOT openclheaders_POPULATED)
      FetchContent_Populate(OpenCLHeaders)
    endif()
    set(PR_OPENCL_HEADERS_DIR "${openclheaders_SOURCE_DIR}")
  endif()

  if(NOT OpenCL_FOUND AND WIN32)
    # NVIDIA CUDA toolkit paths
    foreach(_cl_root
        "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.6"
        "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.5"
        "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.4"
        "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.3"
        "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.2"
        "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.1"
        "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.0"
        "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v11.8")
      if(EXISTS "${_cl_root}/include/CL/cl.h")
        set(OpenCL_INCLUDE_DIR "${_cl_root}/include")
        if(EXISTS "${_cl_root}/lib/x64/OpenCL.lib")
          set(OpenCL_LIBRARY "${_cl_root}/lib/x64/OpenCL.lib")
          set(OpenCL_FOUND TRUE)
        endif()
        break()
      endif()
    endforeach()

    # Driver-only: System32/OpenCL.dll + MinGW import lib from shipped .def
    if(NOT OpenCL_FOUND AND EXISTS "C:/Windows/System32/OpenCL.dll")
      set(PR_OPENCL_DEF "${CMAKE_CURRENT_SOURCE_DIR}/cmake/OpenCL.def")
      set(PR_OPENCL_IMPLIB "${CMAKE_CURRENT_BINARY_DIR}/libOpenCL.a")
      find_program(PR_DLLTOOL NAMES dlltool dlltool.exe)
      if(PR_DLLTOOL AND EXISTS "${PR_OPENCL_DEF}")
        add_custom_command(
          OUTPUT "${PR_OPENCL_IMPLIB}"
          COMMAND ${PR_DLLTOOL} -d "${PR_OPENCL_DEF}" -l "${PR_OPENCL_IMPLIB}" -D OpenCL.dll
          DEPENDS "${PR_OPENCL_DEF}"
          COMMENT "Generating MinGW import library for OpenCL.dll"
          VERBATIM
        )
        add_custom_target(pr_opencl_implib DEPENDS "${PR_OPENCL_IMPLIB}")
        set(OpenCL_LIBRARY "${PR_OPENCL_IMPLIB}")
        if(DEFINED PR_OPENCL_HEADERS_DIR)
          set(OpenCL_INCLUDE_DIR "${PR_OPENCL_HEADERS_DIR}")
        endif()
        if(OpenCL_INCLUDE_DIR OR DEFINED PR_OPENCL_HEADERS_DIR)
          set(OpenCL_FOUND TRUE)
          set(PR_OPENCL_MINGW_IMPLIB TRUE)
          message(STATUS "OpenCL: using System32/OpenCL.dll + MinGW import lib")
        endif()
      endif()
    endif()
  endif()

  if(OpenCL_FOUND OR DEFINED PR_OPENCL_HEADERS_DIR)
    if(TARGET OpenCL::OpenCL AND NOT PR_OPENCL_MINGW_IMPLIB)
      target_link_libraries(pr_core PUBLIC OpenCL::OpenCL)
    else()
      if(DEFINED PR_OPENCL_HEADERS_DIR)
        target_include_directories(pr_core PUBLIC "${PR_OPENCL_HEADERS_DIR}")
      elseif(OpenCL_INCLUDE_DIR)
        target_include_directories(pr_core PUBLIC ${OpenCL_INCLUDE_DIR})
      endif()
      if(OpenCL_LIBRARY)
        target_link_libraries(pr_core PUBLIC ${OpenCL_LIBRARY})
      endif()
      if(PR_OPENCL_MINGW_IMPLIB)
        add_dependencies(pr_core pr_opencl_implib)
        # Ensure the ICD loader is found at runtime
        target_link_options(pr_core PUBLIC "-Wl,-rpath,C:/Windows/System32")
      endif()
    endif()
    target_compile_definitions(pr_core PUBLIC PR_HAS_OPENCL=1)
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
