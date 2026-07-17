// Reference OpenCL kernel (also embedded in gpu_opencl.cpp)
__kernel void segment_clearance(
    __global const uchar *grid,
    const int width,
    const int height,
    const int layer,
    const int net_id,
    const float x_min,
    const float y_min,
    const float grid_mm,
    __global const float *segs,
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
