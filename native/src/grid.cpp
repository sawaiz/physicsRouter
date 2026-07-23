#include "pr_native/grid.hpp"
#include <algorithm>
#include <cmath>

namespace pr {

GridMap::GridMap(double x_min, double x_max, double y_min, double y_max, double grid_mm,
                 int num_layers)
    : x_min_(x_min), x_max_(x_max), y_min_(y_min), y_max_(y_max),
      grid_(std::max(0.05, grid_mm)), layers_(std::max(1, num_layers)) {
  w_ = std::max(1, static_cast<int>(std::ceil((x_max_ - x_min_) / grid_)) + 1);
  h_ = std::max(1, static_cast<int>(std::ceil((y_max_ - y_min_) / grid_)) + 1);
  // Cap grid size for memory — raise when host has headroom (desktop 32 GB).
  // ~8192² × 4 layers × 1 byte ≈ 256 MB for cells alone; still modest on 16 GB+.
  constexpr int kMaxDim = 8192;
  if (w_ > kMaxDim || h_ > kMaxDim) {
    double sx = static_cast<double>(w_) / kMaxDim;
    double sy = static_cast<double>(h_) / kMaxDim;
    double s = std::max(sx, sy);
    grid_ *= s;
    w_ = std::max(1, static_cast<int>(std::ceil((x_max_ - x_min_) / grid_)) + 1);
    h_ = std::max(1, static_cast<int>(std::ceil((y_max_ - y_min_) / grid_)) + 1);
  }
  cells_.assign(static_cast<size_t>(w_) * h_ * layers_, 0);
  hole_cells_.assign(static_cast<size_t>(w_) * h_, 0);
}

void GridMap::clear() {
#ifdef PR_HAS_OPENMP
#pragma omp parallel
  {
#pragma omp sections
    {
#pragma omp section
      std::fill(cells_.begin(), cells_.end(), 0);
#pragma omp section
      std::fill(hole_cells_.begin(), hole_cells_.end(), 0);
    }
  }
#else
  std::fill(cells_.begin(), cells_.end(), 0);
  std::fill(hole_cells_.begin(), hole_cells_.end(), 0);
#endif
}

int GridMap::world_to_ix(double x) const {
  return static_cast<int>(std::floor((x - x_min_) / grid_));
}
int GridMap::world_to_iy(double y) const {
  return static_cast<int>(std::floor((y - y_min_) / grid_));
}
double GridMap::ix_to_world(int ix) const { return x_min_ + (ix + 0.5) * grid_; }
double GridMap::iy_to_world(int iy) const { return y_min_ + (iy + 0.5) * grid_; }

bool GridMap::in_bounds(double x, double y) const {
  return x >= x_min_ && x <= x_max_ && y >= y_min_ && y <= y_max_;
}

void GridMap::paint_rect(double cx, double cy, double w, double h, int layer, int net_id) {
  if (layer < 0 || layer >= layers_)
    return;
  int x0 = world_to_ix(cx - w * 0.5);
  int x1 = world_to_ix(cx + w * 0.5);
  int y0 = world_to_iy(cy - h * 0.5);
  int y1 = world_to_iy(cy + h * 0.5);
  x0 = std::max(0, x0);
  y0 = std::max(0, y0);
  x1 = std::min(w_ - 1, x1);
  y1 = std::min(h_ - 1, y1);
  // Store net_id+1; 255 = blocks all nets
  uint8_t val = net_id < 0 ? 255 : static_cast<uint8_t>(std::min(254, net_id + 1));
  for (int iy = y0; iy <= y1; ++iy) {
    for (int ix = x0; ix <= x1; ++ix) {
      auto &c = cells_[static_cast<size_t>(idx(ix, iy, layer))];
      if (c == 0)
        c = val;
      else if (c != val)
        c = 255; // conflict → blocks all
    }
  }
}

void GridMap::paint_rotated_rect(double cx, double cy, double w, double h,
                                 double rotation_deg, int layer, int net_id) {
  if (layer < 0 || layer >= layers_)
    return;
  double normalized = std::fmod(rotation_deg, 180.0);
  if (std::abs(normalized) < 1e-9) {
    paint_rect(cx, cy, w, h, layer, net_id);
    return;
  }

  constexpr double kPi = 3.14159265358979323846;
  const double angle = rotation_deg * kPi / 180.0;
  const double cosine = std::cos(angle);
  const double sine = std::sin(angle);
  const double aabb_w = std::abs(w * cosine) + std::abs(h * sine);
  const double aabb_h = std::abs(w * sine) + std::abs(h * cosine);
  int x0 = std::max(0, world_to_ix(cx - aabb_w * 0.5));
  int x1 = std::min(w_ - 1, world_to_ix(cx + aabb_w * 0.5));
  int y0 = std::max(0, world_to_iy(cy - aabb_h * 0.5));
  int y1 = std::min(h_ - 1, world_to_iy(cy + aabb_h * 0.5));
  const uint8_t val =
      net_id < 0 ? 255 : static_cast<uint8_t>(std::min(254, net_id + 1));
  const double half_w = w * 0.5;
  const double half_h = h * 0.5;

  for (int iy = y0; iy <= y1; ++iy) {
    for (int ix = x0; ix <= x1; ++ix) {
      const double dx = ix_to_world(ix) - cx;
      const double dy = iy_to_world(iy) - cy;
      // Inverse-rotate the grid-cell centre into pad-local coordinates.
      const double local_x = dx * cosine + dy * sine;
      const double local_y = -dx * sine + dy * cosine;
      if (std::abs(local_x) > half_w || std::abs(local_y) > half_h)
        continue;
      auto &cell = cells_[static_cast<size_t>(idx(ix, iy, layer))];
      if (cell == 0)
        cell = val;
      else if (cell != val)
        cell = 255;
    }
  }
}

void GridMap::paint_trace(double x1, double y1, double x2, double y2, double width_mm,
                          int layer, int net_id) {
  double dx = x2 - x1, dy = y2 - y1;
  double len = std::hypot(dx, dy);
  double step = std::max(width_mm * 0.35, grid_ * 0.5);
  int n = std::max(1, static_cast<int>(std::ceil(len / step)));
  n = std::min(n, 4096);
  for (int i = 0; i <= n; ++i) {
    double t = static_cast<double>(i) / n;
    paint_rect(x1 + dx * t, y1 + dy * t, width_mm, width_mm, layer, net_id);
  }
}

void GridMap::paint_hole_keepout(double x, double y, double radius_mm) {
  const int x0 = std::max(0, world_to_ix(x - radius_mm));
  const int x1 = std::min(w_ - 1, world_to_ix(x + radius_mm));
  const int y0 = std::max(0, world_to_iy(y - radius_mm));
  const int y1 = std::min(h_ - 1, world_to_iy(y + radius_mm));
  const double sample_radius = radius_mm + grid_ * 0.7071067812;
  for (int iy = y0; iy <= y1; ++iy)
    for (int ix = x0; ix <= x1; ++ix)
      if (std::hypot(ix_to_world(ix) - x, iy_to_world(iy) - y) <=
          sample_radius)
        hole_cells_[static_cast<size_t>(iy) * w_ + ix] = 1;
}

bool GridMap::cell_blocked(int ix, int iy, int layer, int net_id) const {
  if (ix < 0 || iy < 0 || ix >= w_ || iy >= h_ || layer < 0 || layer >= layers_)
    return true;
  uint8_t c = cells_[static_cast<size_t>(idx(ix, iy, layer))];
  if (c == 0)
    return false;
  if (c == 255)
    return true;
  return c != static_cast<uint8_t>(net_id + 1);
}

bool GridMap::point_blocked(double x, double y, int layer, int net_id) const {
  if (!in_bounds(x, y))
    return true;
  return cell_blocked(world_to_ix(x), world_to_iy(y), layer, net_id);
}

bool GridMap::disk_blocked(double x, double y, double radius_mm, int layer,
                           int net_id) const {
  if (radius_mm <= 1e-9)
    return point_blocked(x, y, layer, net_id);
  if (!in_bounds(x - radius_mm, y - radius_mm) ||
      !in_bounds(x + radius_mm, y + radius_mm))
    return true;
  const int x0 = std::max(0, world_to_ix(x - radius_mm));
  const int x1 = std::min(w_ - 1, world_to_ix(x + radius_mm));
  const int y0 = std::max(0, world_to_iy(y - radius_mm));
  const int y1 = std::min(h_ - 1, world_to_iy(y + radius_mm));
  // A blocked cell represents a square. Include its half-diagonal so a via
  // cannot slip between cell centres while overlapping fixed copper.
  const double sample_radius = radius_mm + grid_ * 0.7071067812;
  for (int iy = y0; iy <= y1; ++iy) {
    for (int ix = x0; ix <= x1; ++ix) {
      if (std::hypot(ix_to_world(ix) - x, iy_to_world(iy) - y) >
          sample_radius)
        continue;
      if (cell_blocked(ix, iy, layer, net_id))
        return true;
    }
  }
  return false;
}

bool GridMap::hole_blocked(double x, double y) const {
  if (!in_bounds(x, y))
    return true;
  const int ix = world_to_ix(x);
  const int iy = world_to_iy(y);
  if (ix < 0 || iy < 0 || ix >= w_ || iy >= h_)
    return true;
  return hole_cells_[static_cast<size_t>(iy) * w_ + ix] != 0;
}

bool GridMap::segment_blocked(double x1, double y1, double x2, double y2, int layer,
                             int net_id) const {
  if (!in_bounds(x1, y1) || !in_bounds(x2, y2))
    return true;
  double dx = x2 - x1, dy = y2 - y1;
  double len = std::hypot(dx, dy);
  int samples = std::max(1, static_cast<int>(std::ceil(len / (grid_ * 0.5))));
  samples = std::min(samples, 4096);
  for (int i = 0; i <= samples; ++i) {
    double t = static_cast<double>(i) / samples;
    if (point_blocked(x1 + dx * t, y1 + dy * t, layer, net_id))
      return true;
  }
  return false;
}

} // namespace pr
