#include "pr_native/gpu.hpp"
#include <cmath>
#include <cstring>
#include <mutex>
#include <sstream>

#ifdef PR_HAS_OPENCL
#ifdef __APPLE__
#include <OpenCL/opencl.h>
#else
#include <CL/cl.h>
#endif
#include <fstream>
#endif

namespace pr {

#ifdef PR_HAS_OPENCL

static const char *kKernelSrc = R"CLC(
__kernel void segment_clearance(
    __global const uchar *grid,
    const int width,
    const int height,
    const int layer,
    const int net_id,
    const float x_min,
    const float y_min,
    const float grid_mm,
    __global const float *segs, // N * 4
    __global uchar *out_blocked,
    const int nseg)
{
    int i = get_global_id(0);
    if (i >= nseg) return;
    float x1 = segs[i*4+0], y1 = segs[i*4+1];
    float x2 = segs[i*4+2], y2 = segs[i*4+3];
    float dx = x2 - x1, dy = y2 - y1;
    int samples = 8;
    uchar blocked = 0;
    for (int s = 0; s <= samples; ++s) {
        float t = (float)s / (float)samples;
        float x = x1 + dx * t;
        float y = y1 + dy * t;
        int ix = (int)floor((x - x_min) / grid_mm);
        int iy = (int)floor((y - y_min) / grid_mm);
        if (ix < 0 || iy < 0 || ix >= width || iy >= height) { blocked = 1; break; }
        int idx = (layer * height + iy) * width + ix;
        uchar c = grid[idx];
        if (c == 0) continue;
        if (c == 255) { blocked = 1; break; }
        if (c != (uchar)(net_id + 1)) { blocked = 1; break; }
    }
    out_blocked[i] = blocked;
}
)CLC";

static bool g_cl_ok = false;
static cl_context g_ctx = nullptr;
static cl_command_queue g_q = nullptr;
static cl_program g_prog = nullptr;
static cl_kernel g_kern = nullptr;
static std::string g_dev_name;
// The global queue and kernel argument slots are mutable OpenCL objects.
// Bucket variants call the native router from parallel Python workers, so all
// access (including lazy initialization) must be serialized.
static std::mutex g_cl_mutex;

static void cl_init() {
  static bool tried = false;
  if (tried)
    return;
  tried = true;
  cl_platform_id plat = nullptr;
  cl_uint np = 0;
  if (clGetPlatformIDs(1, &plat, &np) != CL_SUCCESS || np == 0)
    return;
  cl_device_id dev = nullptr;
  cl_uint nd = 0;
  if (clGetDeviceIDs(plat, CL_DEVICE_TYPE_GPU, 1, &dev, &nd) != CL_SUCCESS || nd == 0) {
    if (clGetDeviceIDs(plat, CL_DEVICE_TYPE_DEFAULT, 1, &dev, &nd) != CL_SUCCESS || nd == 0)
      return;
  }
  char name[256] = {0};
  clGetDeviceInfo(dev, CL_DEVICE_NAME, sizeof(name), name, nullptr);
  g_dev_name = name;
  cl_int err = 0;
  g_ctx = clCreateContext(nullptr, 1, &dev, nullptr, nullptr, &err);
  if (err != CL_SUCCESS)
    return;
  g_q = clCreateCommandQueue(g_ctx, dev, 0, &err);
  if (err != CL_SUCCESS)
    return;
  const char *src = kKernelSrc;
  size_t len = strlen(src);
  g_prog = clCreateProgramWithSource(g_ctx, 1, &src, &len, &err);
  if (err != CL_SUCCESS)
    return;
  if (clBuildProgram(g_prog, 1, &dev, nullptr, nullptr, nullptr) != CL_SUCCESS)
    return;
  g_kern = clCreateKernel(g_prog, "segment_clearance", &err);
  if (err != CL_SUCCESS)
    return;
  g_cl_ok = true;
}

#endif

GpuStatus gpu_probe() {
  GpuStatus s;
#ifdef PR_HAS_OPENCL
  {
    std::lock_guard<std::mutex> lock(g_cl_mutex);
    cl_init();
    if (g_cl_ok) {
      s.available = true;
      s.backend = "opencl";
      s.device_name = g_dev_name;
      return s;
    }
  }
#endif
  s.available = false;
  s.backend = "none";
  s.device_name = "cpu";
  return s;
}

std::vector<uint8_t> batch_segment_clearance(const GridMap &grid, int layer, int net_id,
                                             const std::vector<double> &segments_xyxy,
                                             bool prefer_gpu) {
  int n = static_cast<int>(segments_xyxy.size() / 4);
  std::vector<uint8_t> out(static_cast<size_t>(std::max(n, 0)), 0);
  if (n <= 0)
    return out;

#ifdef PR_HAS_OPENCL
  if (prefer_gpu) {
    std::lock_guard<std::mutex> lock(g_cl_mutex);
    cl_init();
    if (g_cl_ok) {
      cl_int err = 0;
      size_t gbytes = grid.data().size();
      cl_mem gbuf =
          clCreateBuffer(g_ctx, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR, gbytes,
                         const_cast<uint8_t *>(grid.data().data()), &err);
      std::vector<float> segs(static_cast<size_t>(n) * 4);
      for (int i = 0; i < n * 4; ++i)
        segs[static_cast<size_t>(i)] = static_cast<float>(segments_xyxy[static_cast<size_t>(i)]);
      cl_mem sbuf = clCreateBuffer(g_ctx, CL_MEM_READ_ONLY | CL_MEM_COPY_HOST_PTR,
                                   segs.size() * sizeof(float), segs.data(), &err);
      cl_mem obuf = clCreateBuffer(g_ctx, CL_MEM_WRITE_ONLY, out.size(), nullptr, &err);
      int w = grid.width(), h = grid.height();
      float xmin = static_cast<float>(grid.x_min());
      float ymin = static_cast<float>(grid.y_min());
      float gmm = static_cast<float>(grid.grid());
      clSetKernelArg(g_kern, 0, sizeof(cl_mem), &gbuf);
      clSetKernelArg(g_kern, 1, sizeof(int), &w);
      clSetKernelArg(g_kern, 2, sizeof(int), &h);
      clSetKernelArg(g_kern, 3, sizeof(int), &layer);
      clSetKernelArg(g_kern, 4, sizeof(int), &net_id);
      clSetKernelArg(g_kern, 5, sizeof(float), &xmin);
      clSetKernelArg(g_kern, 6, sizeof(float), &ymin);
      clSetKernelArg(g_kern, 7, sizeof(float), &gmm);
      clSetKernelArg(g_kern, 8, sizeof(cl_mem), &sbuf);
      clSetKernelArg(g_kern, 9, sizeof(cl_mem), &obuf);
      clSetKernelArg(g_kern, 10, sizeof(int), &n);
      size_t global = static_cast<size_t>(n);
      if (clEnqueueNDRangeKernel(g_q, g_kern, 1, nullptr, &global, nullptr, 0, nullptr,
                                 nullptr) == CL_SUCCESS) {
        clEnqueueReadBuffer(g_q, obuf, CL_TRUE, 0, out.size(), out.data(), 0, nullptr, nullptr);
        clReleaseMemObject(gbuf);
        clReleaseMemObject(sbuf);
        clReleaseMemObject(obuf);
        return out;
      }
      clReleaseMemObject(gbuf);
      clReleaseMemObject(sbuf);
      clReleaseMemObject(obuf);
    }
  }
#endif

  // CPU OpenMP fallback
#ifdef PR_HAS_OPENMP
#pragma omp parallel for schedule(static)
#endif
  for (int i = 0; i < n; ++i) {
    double x1 = segments_xyxy[static_cast<size_t>(i) * 4 + 0];
    double y1 = segments_xyxy[static_cast<size_t>(i) * 4 + 1];
    double x2 = segments_xyxy[static_cast<size_t>(i) * 4 + 2];
    double y2 = segments_xyxy[static_cast<size_t>(i) * 4 + 3];
    out[static_cast<size_t>(i)] =
        grid.segment_blocked(x1, y1, x2, y2, layer, net_id) ? 1 : 0;
  }
  return out;
}

} // namespace pr
