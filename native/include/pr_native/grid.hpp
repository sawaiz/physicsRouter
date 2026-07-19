#pragma once
#include "types.hpp"
#include <cstdint>
#include <vector>

namespace pr {

/** Packed occupancy grid — much faster than rect-list segment tests. */
class GridMap {
public:
  GridMap() = default;
  GridMap(double x_min, double x_max, double y_min, double y_max, double grid_mm,
          int num_layers);

  void clear();
  void paint_rect(double cx, double cy, double w, double h, int layer, int net_id);
  void paint_rotated_rect(double cx, double cy, double w, double h,
                          double rotation_deg, int layer, int net_id);
  void paint_trace(double x1, double y1, double x2, double y2, double width_mm, int layer,
                   int net_id);
  void paint_hole_keepout(double x, double y, double radius_mm);

  bool in_bounds(double x, double y) const;
  bool cell_blocked(int ix, int iy, int layer, int net_id) const;
  bool point_blocked(double x, double y, int layer, int net_id) const;
  bool disk_blocked(double x, double y, double radius_mm, int layer,
                    int net_id) const;
  bool hole_blocked(double x, double y) const;
  bool segment_blocked(double x1, double y1, double x2, double y2, int layer,
                       int net_id) const;

  int world_to_ix(double x) const;
  int world_to_iy(double y) const;
  double ix_to_world(int ix) const;
  double iy_to_world(int iy) const;

  int width() const { return w_; }
  int height() const { return h_; }
  int layers() const { return layers_; }
  double grid() const { return grid_; }
  double x_min() const { return x_min_; }
  double y_min() const { return y_min_; }
  double x_max() const { return x_max_; }
  double y_max() const { return y_max_; }

  /** Flat occupancy: layer-major, then row-major. 0 = free, else net_id+1 (or 255 all). */
  const std::vector<uint8_t> &data() const { return cells_; }
  std::vector<uint8_t> &data() { return cells_; }

private:
  int idx(int ix, int iy, int layer) const {
    return (layer * h_ + iy) * w_ + ix;
  }
  double x_min_ = 0, x_max_ = 1, y_min_ = 0, y_max_ = 1, grid_ = 0.5;
  int w_ = 1, h_ = 1, layers_ = 1;
  std::vector<uint8_t> cells_;
  std::vector<uint8_t> hole_cells_;
};

} // namespace pr
