#pragma once
#include "grid.hpp"
#include <string>
#include <utility>
#include <vector>

namespace pr {

struct GpuStatus {
  bool available = false;
  std::string device_name;
  std::string backend; // opencl | none
};

GpuStatus gpu_probe();

/**
 * Batch segment clearance tests on GPU (OpenCL) when available.
 * segments: flat [x1,y1,x2,y2] * N
 * Returns blocked flags (1 = blocked).
 * Falls back to multi-threaded CPU if GPU unavailable.
 */
std::vector<uint8_t> batch_segment_clearance(
    const GridMap &grid, int layer, int net_id,
    const std::vector<double> &segments_xyxy, bool prefer_gpu = true);

} // namespace pr
