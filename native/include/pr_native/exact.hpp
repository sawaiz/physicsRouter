#pragma once
#include "types.hpp"
#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

namespace pr {

/** Exact-geometry clearance authority (port of the Python ObstacleMap).
 *
 * Rect obstacles (already inflated by the caller) in a 2 mm spatial hash with
 * exact Liang–Barsky segment tests, plus painted copper segments checked with
 * continuous segment–segment distance. net -1 blocks every net (None in
 * Python); otherwise same-net geometry is ignored.
 */

struct ExRect {
  double cx = 0, cy = 0, w = 0, h = 0;
  int net = -1; // -1 = blocks all nets
};

struct ExPaint {
  double x1 = 0, y1 = 0, x2 = 0, y2 = 0;
  double width = 0.25;
  int net = 0;
};

/** Per-layer congestion snapshot (negotiated congestion, marshalled from Python). */
struct CongestionView {
  double cell_mm = 1.0;
  std::unordered_map<int64_t, double> cells;

  bool empty() const { return cells.empty(); }
  double cost(double x, double y) const;
  /** length + mean sampled cell cost (mirrors CongestionMap.edge_cost). */
  double edge_cost(double x1, double y1, double x2, double y2) const;
};

class ExactMap {
public:
  ExactMap(double x_min, double x_max, double y_min, double y_max, double clearance_mm,
           int num_layers);

  void ensure_layer(int layer);
  /** Rect must arrive already clearance-inflated (Python owns inflation policy). */
  void add_rect(double cx, double cy, double w, double h, int layer, int net);
  /** Record exact copper segment (keepout rects are added separately via add_rect). */
  void add_painted(double x1, double y1, double x2, double y2, int layer, double width,
                   int net);

  bool in_bounds(double x, double y) const;
  bool blocked(double x, double y, int layer, int net) const;
  bool segment_blocked(double x1, double y1, double x2, double y2, int layer, int net,
                       double width_mm) const;

  const std::vector<ExRect> &rects(int layer) const;
  double clearance() const { return clearance_; }
  double x_min() const { return x_min_; }
  double x_max() const { return x_max_; }
  double y_min() const { return y_min_; }
  double y_max() const { return y_max_; }

private:
  static constexpr double kCell = 2.0;
  int64_t key(int i, int j) const { return (static_cast<int64_t>(i) << 32) ^ (j & 0xffffffffLL); }

  double x_min_, x_max_, y_min_, y_max_, clearance_;
  std::vector<std::vector<ExRect>> rects_;
  std::vector<std::vector<ExPaint>> paints_;
  std::vector<std::unordered_map<int64_t, std::vector<int32_t>>> cells_;
  // Painted segs in the same spatial hash (bbox inflated to cover clearance
  // + realistic query widths, so cell-local queries stay exact)
  std::vector<std::unordered_map<int64_t, std::vector<int32_t>>> pcells_;
  std::vector<ExRect> empty_rects_;
};

/** Line-of-sight shortcutting against the exact map (Python _rubberband). */
std::vector<Vec2> rubberband_exact(const ExactMap &m, const std::vector<Vec2> &path,
                                   int layer, int net, double width_mm);

/** Full free-angle search: LOS → isotropic detours + radar → 1/2/3-corner →
 * hierarchical multi-grid A* (16-dir on fine grids). Sets *method_out to one of
 * los | detour | detour2 | detour3 | astar. Empty result = no path. */
std::vector<Vec2> free_angle_route_exact(const ExactMap &m, Vec2 start, Vec2 goal, int layer,
                                         int net, double grid_mm, int max_expansions,
                                         double width_mm, const CongestionView *congestion,
                                         std::string *method_out);

} // namespace pr
