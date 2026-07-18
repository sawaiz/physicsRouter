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
  // Cap grid size for memory
  constexpr int kMaxDim = 4096;
  if (w_ > kMaxDim || h_ > kMaxDim) {
    double sx = static_cast<double>(w_) / kMaxDim;
    double sy = static_cast<double>(h_) / kMaxDim;
    double s = std::max(sx, sy);
    grid_ *= s;
    w_ = std::max(1, static_cast<int>(std::ceil((x_max_ - x_min_) / grid_)) + 1);
    h_ = std::max(1, static_cast<int>(std::ceil((y_max_ - y_min_) / grid_)) + 1);
  }
  cells_.assign(static_cast<size_t>(w_) * h_ * layers_, 0);
}

void GridMap::clear() { std::fill(cells_.begin(), cells_.end(), 0); }

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
