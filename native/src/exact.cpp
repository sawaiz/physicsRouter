#include "pr_native/exact.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <queue>
#include <unordered_set>

namespace pr {

namespace {

inline double dist(double x1, double y1, double x2, double y2) {
  return std::hypot(x2 - x1, y2 - y1);
}
inline double dist(const Vec2 &a, const Vec2 &b) { return dist(a.x, a.y, b.x, b.y); }

inline bool point_in_rect(double px, double py, const ExRect &r) {
  return std::abs(px - r.cx) <= r.w / 2 && std::abs(py - r.cy) <= r.h / 2;
}

/** Exact segment vs axis-aligned rect (Liang–Barsky clip) — port of Python. */
bool segment_hits_rect(double x1, double y1, double x2, double y2, const ExRect &r) {
  const double hw = r.w / 2, hh = r.h / 2;
  const double rx1 = x1 - r.cx, ry1 = y1 - r.cy;
  const double dx = x2 - x1, dy = y2 - y1;
  double t0 = 0.0, t1 = 1.0;
  const double p[4] = {-dx, dx, -dy, dy};
  const double q[4] = {rx1 + hw, hw - rx1, ry1 + hh, hh - ry1};
  for (int i = 0; i < 4; ++i) {
    if (p[i] > -1e-12 && p[i] < 1e-12) {
      if (q[i] < 0.0)
        return false;
      continue;
    }
    const double t = q[i] / p[i];
    if (p[i] < 0.0) {
      if (t > t1)
        return false;
      if (t > t0)
        t0 = t;
    } else {
      if (t < t0)
        return false;
      if (t < t1)
        t1 = t;
    }
  }
  return t0 <= t1;
}

double point_seg_dist(double px, double py, double x1, double y1, double x2, double y2) {
  const double dx = x2 - x1, dy = y2 - y1;
  const double len2 = dx * dx + dy * dy;
  if (len2 < 1e-18)
    return dist(px, py, x1, y1);
  double t = ((px - x1) * dx + (py - y1) * dy) / len2;
  t = std::max(0.0, std::min(1.0, t));
  return dist(px, py, x1 + t * dx, y1 + t * dy);
}

bool segs_intersect(double ax1, double ay1, double ax2, double ay2, double bx1, double by1,
                    double bx2, double by2) {
  auto orient = [](double ox, double oy, double px, double py, double qx, double qy) {
    const double v = (px - ox) * (qy - oy) - (py - oy) * (qx - ox);
    if (v > 1e-12)
      return 1;
    if (v < -1e-12)
      return -1;
    return 0;
  };
  const int o1 = orient(ax1, ay1, ax2, ay2, bx1, by1);
  const int o2 = orient(ax1, ay1, ax2, ay2, bx2, by2);
  const int o3 = orient(bx1, by1, bx2, by2, ax1, ay1);
  const int o4 = orient(bx1, by1, bx2, by2, ax2, ay2);
  return (o1 != o2 && o3 != o4);
}

double seg_seg_min_dist(double ax1, double ay1, double ax2, double ay2, double bx1, double by1,
                        double bx2, double by2) {
  if (segs_intersect(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2))
    return 0.0;
  double d = point_seg_dist(ax1, ay1, bx1, by1, bx2, by2);
  d = std::min(d, point_seg_dist(ax2, ay2, bx1, by1, bx2, by2));
  d = std::min(d, point_seg_dist(bx1, by1, ax1, ay1, ax2, ay2));
  d = std::min(d, point_seg_dist(bx2, by2, ax1, ay1, ax2, ay2));
  return d;
}

} // namespace

// ---------------------------------------------------------------------------
// CongestionView
// ---------------------------------------------------------------------------

double CongestionView::cost(double x, double y) const {
  const double c = cell_mm;
  const int64_t k = (static_cast<int64_t>(std::floor(x / c)) << 32) ^
                    (static_cast<int64_t>(std::floor(y / c)) & 0xffffffffLL);
  auto it = cells.find(k);
  return it == cells.end() ? 0.0 : it->second;
}

double CongestionView::edge_cost(double x1, double y1, double x2, double y2) const {
  const double length = dist(x1, y1, x2, y2);
  double cong = 0.0;
  const int samples = std::max(1, static_cast<int>(length / std::max(cell_mm, 0.2)));
  for (int i = 0; i <= samples; ++i) {
    const double t = static_cast<double>(i) / samples;
    cong += cost(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t);
  }
  cong /= samples + 1;
  return length + cong;
}

// ---------------------------------------------------------------------------
// ExactMap
// ---------------------------------------------------------------------------

ExactMap::ExactMap(double x_min, double x_max, double y_min, double y_max, double clearance_mm,
                   int num_layers)
    : x_min_(x_min), x_max_(x_max), y_min_(y_min), y_max_(y_max), clearance_(clearance_mm) {
  const int n = std::max(1, num_layers);
  rects_.resize(n);
  paints_.resize(n);
  cells_.resize(n);
  pcells_.resize(n);
}

void ExactMap::ensure_layer(int layer) {
  if (layer < 0)
    return;
  if (static_cast<size_t>(layer) >= rects_.size()) {
    rects_.resize(layer + 1);
    paints_.resize(layer + 1);
    cells_.resize(layer + 1);
    pcells_.resize(layer + 1);
  }
}

void ExactMap::add_rect(double cx, double cy, double w, double h, int layer, int net) {
  ensure_layer(layer);
  if (layer < 0)
    return;
  auto &rl = rects_[layer];
  rl.push_back(ExRect{cx, cy, w, h, net});
  const int32_t idx = static_cast<int32_t>(rl.size()) - 1;
  auto &cm = cells_[layer];
  const int i0 = static_cast<int>(std::floor((cx - w / 2 - 1e-9) / kCell));
  const int i1 = static_cast<int>(std::floor((cx + w / 2 + 1e-9) / kCell));
  const int j0 = static_cast<int>(std::floor((cy - h / 2 - 1e-9) / kCell));
  const int j1 = static_cast<int>(std::floor((cy + h / 2 + 1e-9) / kCell));
  for (int i = i0; i <= i1; ++i)
    for (int j = j0; j <= j1; ++j)
      cm[key(i, j)].push_back(idx);
}

void ExactMap::add_painted(double x1, double y1, double x2, double y2, int layer, double width,
                           int net) {
  ensure_layer(layer);
  if (layer < 0)
    return;
  auto &pl = paints_[layer];
  pl.push_back(ExPaint{x1, y1, x2, y2, width, net});
  const int32_t idx = static_cast<int32_t>(pl.size()) - 1;
  // Inflate registration so a cell-local query is exact for query widths ≤ ~3 mm
  const double pad = clearance_ + 0.5 * width + 1.5;
  auto &cm = pcells_[layer];
  const int i0 = static_cast<int>(std::floor((std::min(x1, x2) - pad) / kCell));
  const int i1 = static_cast<int>(std::floor((std::max(x1, x2) + pad) / kCell));
  const int j0 = static_cast<int>(std::floor((std::min(y1, y2) - pad) / kCell));
  const int j1 = static_cast<int>(std::floor((std::max(y1, y2) + pad) / kCell));
  for (int i = i0; i <= i1; ++i)
    for (int j = j0; j <= j1; ++j)
      cm[key(i, j)].push_back(idx);
}

void ExactMap::set_outline(const std::vector<Vec2> &poly) {
  outline_.clear();
  if (poly.size() < 3)
    return;
  outline_ = poly;
  // Ensure closed ring for edge iteration (duplicate first at end if needed)
  if (dist(outline_.front(), outline_.back()) > 1e-9)
    outline_.push_back(outline_.front());
}

bool ExactMap::point_in_outline(double x, double y) const {
  if (outline_.size() < 4) // need closed ring ≥ 3 unique verts
    return true;
  // Even-odd ray cast (+x)
  bool inside = false;
  const size_t n = outline_.size();
  for (size_t i = 0, j = n - 1; i < n; j = i++) {
    const double xi = outline_[i].x, yi = outline_[i].y;
    const double xj = outline_[j].x, yj = outline_[j].y;
    const bool intersect =
        ((yi > y) != (yj > y)) &&
        (x < (xj - xi) * (y - yi) / ((yj - yi) + 1e-30) + xi);
    if (intersect)
      inside = !inside;
  }
  return inside;
}

bool ExactMap::segment_outside_outline(double x1, double y1, double x2, double y2) const {
  if (outline_.size() < 4)
    return false;
  if (!point_in_outline(x1, y1) || !point_in_outline(x2, y2))
    return true;
  // Concave-safe: proper intersection with any outline edge ⇒ leaves interior
  const size_t n = outline_.size();
  for (size_t i = 0; i + 1 < n; ++i) {
    const double ox1 = outline_[i].x, oy1 = outline_[i].y;
    const double ox2 = outline_[i + 1].x, oy2 = outline_[i + 1].y;
    // Skip if shares an endpoint with the query (touching boundary OK)
    const bool share =
        (dist(x1, y1, ox1, oy1) < 1e-9 || dist(x1, y1, ox2, oy2) < 1e-9 ||
         dist(x2, y2, ox1, oy1) < 1e-9 || dist(x2, y2, ox2, oy2) < 1e-9);
    if (share)
      continue;
    if (segs_intersect(x1, y1, x2, y2, ox1, oy1, ox2, oy2))
      return true;
  }
  // Midpoint sample (cheap extra safety for grazing chords)
  const double mx = 0.5 * (x1 + x2), my = 0.5 * (y1 + y2);
  if (!point_in_outline(mx, my))
    return true;
  return false;
}

bool ExactMap::in_bounds(double x, double y) const {
  if (!(x_min_ <= x && x <= x_max_ && y_min_ <= y && y <= y_max_))
    return false;
  if (has_outline() && !point_in_outline(x, y))
    return false;
  return true;
}

const std::vector<ExRect> &ExactMap::rects(int layer) const {
  if (layer < 0 || static_cast<size_t>(layer) >= rects_.size())
    return empty_rects_;
  return rects_[layer];
}

bool ExactMap::blocked(double x, double y, int layer, int net) const {
  if (!in_bounds(x, y))
    return true;
  if (layer < 0 || static_cast<size_t>(layer) >= rects_.size())
    return false;
  const auto &cm = cells_[layer];
  const auto it = cm.find(key(static_cast<int>(std::floor(x / kCell)),
                              static_cast<int>(std::floor(y / kCell))));
  if (it != cm.end()) {
    const auto &rl = rects_[layer];
    for (int32_t idx : it->second) {
      const ExRect &r = rl[idx];
      if (r.net >= 0 && r.net == net)
        continue;
      if (point_in_rect(x, y, r))
        return true;
    }
  }
  const auto &pm = pcells_[layer];
  const auto pit = pm.find(key(static_cast<int>(std::floor(x / kCell)),
                               static_cast<int>(std::floor(y / kCell))));
  if (pit != pm.end()) {
    const auto &pl = paints_[layer];
    for (int32_t idx : pit->second) {
      const ExPaint &ps = pl[idx];
      if (ps.net == net)
        continue;
      const double need = clearance_ + 0.5 * ps.width;
      if (x < std::min(ps.x1, ps.x2) - need || x > std::max(ps.x1, ps.x2) + need ||
          y < std::min(ps.y1, ps.y2) - need || y > std::max(ps.y1, ps.y2) + need)
        continue;
      if (point_seg_dist(x, y, ps.x1, ps.y1, ps.x2, ps.y2) < need)
        return true;
    }
  }
  return false;
}

bool ExactMap::segment_blocked(double x1, double y1, double x2, double y2, int layer, int net,
                               double width_mm) const {
  if (!(in_bounds(x1, y1) && in_bounds(x2, y2)))
    return true;
  if (segment_outside_outline(x1, y1, x2, y2))
    return true;
  if (layer < 0 || static_cast<size_t>(layer) >= rects_.size())
    return false;
  const auto &cm = cells_[layer];
  const auto &rl = rects_[layer];
  const double length = dist(x1, y1, x2, y2);

  // Fast path for short segments (A* edges): single 3x3 neighbourhood around
  // the midpoint, no dedupe sets — duplicate rect tests are cheaper than hashing.
  if (length <= kCell * 0.5) {
    const double mx = (x1 + x2) * 0.5, my = (y1 + y2) * 0.5;
    const int ci = static_cast<int>(std::floor(mx / kCell));
    const int cj = static_cast<int>(std::floor(my / kCell));
    for (int di = -1; di <= 1; ++di) {
      for (int dj = -1; dj <= 1; ++dj) {
        const auto it = cm.find(key(ci + di, cj + dj));
        if (it == cm.end())
          continue;
        for (int32_t idx : it->second) {
          const ExRect &r = rl[idx];
          if (r.net >= 0 && r.net == net)
            continue;
          if (segment_hits_rect(x1, y1, x2, y2, r))
            return true;
        }
      }
    }
    const auto &pm_f = pcells_[layer];
    const auto &pl_f = paints_[layer];
    const auto pit = pm_f.find(key(ci, cj));
    if (pit != pm_f.end()) {
      for (int32_t idx : pit->second) {
        const ExPaint &ps = pl_f[idx];
        if (ps.net == net)
          continue;
        const double need = clearance_ + 0.5 * (width_mm + ps.width);
        if (std::max(ps.x1, ps.x2) < std::min(x1, x2) - need ||
            std::min(ps.x1, ps.x2) > std::max(x1, x2) + need ||
            std::max(ps.y1, ps.y2) < std::min(y1, y2) - need ||
            std::min(ps.y1, ps.y2) > std::max(y1, y2) + need)
          continue;
        if (seg_seg_min_dist(x1, y1, x2, y2, ps.x1, ps.y1, ps.x2, ps.y2) < need)
          return true;
      }
    }
    return false;
  }

  // Walk at half-cell step; 3x3 neighbourhood covers every traversed cell.
  const int steps = std::max(1, static_cast<int>(std::ceil(length / (kCell * 0.5))));
  std::unordered_set<int32_t> seen;
  std::unordered_set<int64_t> visited;
  for (int s = 0; s <= steps; ++s) {
    const double t = static_cast<double>(s) / steps;
    const int ci = static_cast<int>(std::floor((x1 + (x2 - x1) * t) / kCell));
    const int cj = static_cast<int>(std::floor((y1 + (y2 - y1) * t) / kCell));
    for (int di = -1; di <= 1; ++di) {
      for (int dj = -1; dj <= 1; ++dj) {
        const int64_t k = key(ci + di, cj + dj);
        if (!visited.insert(k).second)
          continue;
        const auto it = cm.find(k);
        if (it == cm.end())
          continue;
        for (int32_t idx : it->second) {
          if (!seen.insert(idx).second)
            continue;
          const ExRect &r = rl[idx];
          if (r.net >= 0 && r.net == net)
            continue;
          if (segment_hits_rect(x1, y1, x2, y2, r))
            return true;
        }
      }
    }
  }
  const double bx0 = std::min(x1, x2), bx1 = std::max(x1, x2);
  const double by0 = std::min(y1, y2), by1 = std::max(y1, y2);
  const auto &pm = pcells_[layer];
  const auto &pl = paints_[layer];
  std::unordered_set<int32_t> pseen;
  for (const int64_t k : visited) {
    const auto it = pm.find(k);
    if (it == pm.end())
      continue;
    for (int32_t idx : it->second) {
      if (!pseen.insert(idx).second)
        continue;
      const ExPaint &ps = pl[idx];
      if (ps.net == net)
        continue;
      const double need = clearance_ + 0.5 * (width_mm + ps.width);
      if (std::max(ps.x1, ps.x2) < bx0 - need || std::min(ps.x1, ps.x2) > bx1 + need ||
          std::max(ps.y1, ps.y2) < by0 - need || std::min(ps.y1, ps.y2) > by1 + need)
        continue;
      if (seg_seg_min_dist(x1, y1, x2, y2, ps.x1, ps.y1, ps.x2, ps.y2) < need)
        return true;
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// Built-in DRC (always-on: the router validates its own copper)
// ---------------------------------------------------------------------------

std::vector<DrcViolation> drc_check(const std::vector<DrcSeg> &segs,
                                    const std::vector<DrcVia> &vias, double clearance_mm,
                                    int max_violations) {
  std::vector<DrcViolation> out;
  if (max_violations <= 0)
    max_violations = 200;
  constexpr double kCell = 2.0;
  auto cell_key = [](int i, int j, int layer) {
    return (static_cast<int64_t>(layer) << 52) ^ (static_cast<int64_t>(i & 0x3ffffff) << 26) ^
           static_cast<int64_t>(j & 0x3ffffff);
  };

  // Hash segments by inflated bbox cells per layer
  std::unordered_map<int64_t, std::vector<int32_t>> shash;
  for (size_t si = 0; si < segs.size(); ++si) {
    const DrcSeg &s = segs[si];
    const double pad = clearance_mm + s.width + 1.0;
    const int i0 = static_cast<int>(std::floor((std::min(s.x1, s.x2) - pad) / kCell));
    const int i1 = static_cast<int>(std::floor((std::max(s.x1, s.x2) + pad) / kCell));
    const int j0 = static_cast<int>(std::floor((std::min(s.y1, s.y2) - pad) / kCell));
    const int j1 = static_cast<int>(std::floor((std::max(s.y1, s.y2) + pad) / kCell));
    for (int i = i0; i <= i1; ++i)
      for (int j = j0; j <= j1; ++j)
        shash[cell_key(i, j, s.layer)].push_back(static_cast<int32_t>(si));
  }

  // Seg vs seg: same layer, foreign nets (each unordered pair once)
  std::unordered_set<int64_t> pair_seen;
  for (size_t ai = 0; ai < segs.size() && out.size() < static_cast<size_t>(max_violations);
       ++ai) {
    const DrcSeg &a = segs[ai];
    const int ci0 = static_cast<int>(std::floor(std::min(a.x1, a.x2) / kCell));
    const int ci1 = static_cast<int>(std::floor(std::max(a.x1, a.x2) / kCell));
    const int cj0 = static_cast<int>(std::floor(std::min(a.y1, a.y2) / kCell));
    const int cj1 = static_cast<int>(std::floor(std::max(a.y1, a.y2) / kCell));
    for (int i = ci0; i <= ci1; ++i) {
      for (int j = cj0; j <= cj1; ++j) {
        const auto it = shash.find(cell_key(i, j, a.layer));
        if (it == shash.end())
          continue;
        for (int32_t bi : it->second) {
          if (static_cast<size_t>(bi) <= ai)
            continue;
          const DrcSeg &b = segs[bi];
          if (a.net == b.net)
            continue;
          const int64_t pk = (static_cast<int64_t>(ai) << 32) ^ bi;
          if (!pair_seen.insert(pk).second)
            continue;
          const double d =
              seg_seg_min_dist(a.x1, a.y1, a.x2, a.y2, b.x1, b.y1, b.x2, b.y2);
          const double copper = 0.5 * (a.width + b.width);
          const double need = clearance_mm + copper;
          if (d < need) {
            DrcViolation v;
            v.kind = d < copper ? 1 : 0;
            v.net_a = a.net;
            v.net_b = b.net;
            v.layer = a.layer;
            v.x = (a.x1 + a.x2) * 0.5;
            v.y = (a.y1 + a.y2) * 0.5;
            v.dist = d;
            v.need = need;
            out.push_back(v);
            if (out.size() >= static_cast<size_t>(max_violations))
              return out;
          }
        }
      }
    }
  }

  // Via vs seg (through vias: every layer) and via vs via
  for (size_t vi = 0; vi < vias.size() && out.size() < static_cast<size_t>(max_violations);
       ++vi) {
    const DrcVia &v = vias[vi];
    for (size_t si = 0; si < segs.size(); ++si) {
      const DrcSeg &s = segs[si];
      if (s.net == v.net)
        continue;
      const double d = point_seg_dist(v.x, v.y, s.x1, s.y1, s.x2, s.y2);
      const double copper = 0.5 * (v.size + s.width);
      const double need = clearance_mm + copper;
      if (d < need) {
        DrcViolation viol;
        viol.kind = d < copper ? 1 : 0;
        viol.net_a = v.net;
        viol.net_b = s.net;
        viol.layer = s.layer;
        viol.x = v.x;
        viol.y = v.y;
        viol.dist = d;
        viol.need = need;
        out.push_back(viol);
        if (out.size() >= static_cast<size_t>(max_violations))
          return out;
      }
    }
    for (size_t wi = vi + 1; wi < vias.size(); ++wi) {
      const DrcVia &w = vias[wi];
      if (w.net == v.net)
        continue;
      const double d = dist(v.x, v.y, w.x, w.y);
      const double copper = 0.5 * (v.size + w.size);
      const double need = clearance_mm + copper;
      if (d < need) {
        DrcViolation viol;
        viol.kind = d < copper ? 1 : 0;
        viol.net_a = v.net;
        viol.net_b = w.net;
        viol.layer = -1;
        viol.x = (v.x + w.x) * 0.5;
        viol.y = (v.y + w.y) * 0.5;
        viol.dist = d;
        viol.need = need;
        out.push_back(viol);
        if (out.size() >= static_cast<size_t>(max_violations))
          return out;
      }
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Rubberband (Python _rubberband port)
// ---------------------------------------------------------------------------

std::vector<Vec2> rubberband_exact(const ExactMap &m, const std::vector<Vec2> &path, int layer,
                                   int net, double width_mm) {
  if (path.size() <= 2)
    return path;
  std::vector<Vec2> out{path.front()};
  size_t i = 0;
  while (i < path.size() - 1) {
    size_t j = path.size() - 1;
    bool advanced = false;
    while (j > i + 1) {
      const Vec2 &a = out.back();
      const Vec2 &b = path[j];
      if (!m.segment_blocked(a.x, a.y, b.x, b.y, layer, net, width_mm)) {
        out.push_back(b);
        i = j;
        advanced = true;
        break;
      }
      --j;
    }
    if (!advanced) {
      ++i;
      if (i < path.size() &&
          (out.back().x != path[i].x || out.back().y != path[i].y))
        out.push_back(path[i]);
    }
  }
  if (out.back().x != path.back().x || out.back().y != path.back().y)
    out.push_back(path.back());
  return out;
}

// ---------------------------------------------------------------------------
// Radar scan (topology.radar_scan_points port)
// ---------------------------------------------------------------------------

namespace {

std::vector<Vec2> radar_scan(const ExactMap &m, Vec2 origin, Vec2 goal, int layer, int net,
                             int rays, double grid_mm) {
  const double span = dist(origin, goal);
  const double rmax = std::max(span * 1.4, 12.0);
  std::vector<Vec2> pts;

  for (double t : {0.25, 0.5, 0.75})
    pts.push_back({origin.x + (goal.x - origin.x) * t, origin.y + (goal.y - origin.y) * t});

  for (int i = 0; i < rays; ++i) {
    const double ang = 2.0 * M_PI * i / rays;
    const double dx = std::cos(ang), dy = std::sin(ang);
    bool prev_free = true;
    const double step = std::max(grid_mm, 0.35);
    double d = step;
    while (d <= rmax) {
      const double x = origin.x + dx * d, y = origin.y + dy * d;
      if (!m.in_bounds(x, y))
        break;
      const bool free = !m.blocked(x, y, layer, net);
      if (prev_free && !free) {
        const double bx = origin.x + dx * (d - step), by = origin.y + dy * (d - step);
        if (m.in_bounds(bx, by) && !m.blocked(bx, by, layer, net))
          pts.push_back({bx, by});
        break;
      }
      if (free && d > span * 0.3) {
        if (static_cast<int>(d / step) % 4 == 0)
          pts.push_back({x, y});
      }
      prev_free = free;
      d += step;
    }
  }

  const double reach = rmax;
  for (const ExRect &ob : m.rects(layer)) {
    if (ob.net >= 0 && ob.net == net)
      continue;
    if (dist(ob.cx, ob.cy, origin.x, origin.y) > reach &&
        dist(ob.cx, ob.cy, goal.x, goal.y) > reach)
      continue;
    const double hw = ob.w / 2 + grid_mm * 1.2, hh = ob.h / 2 + grid_mm * 1.2;
    const Vec2 corners[4] = {{ob.cx - hw, ob.cy - hh},
                             {ob.cx + hw, ob.cy - hh},
                             {ob.cx - hw, ob.cy + hh},
                             {ob.cx + hw, ob.cy + hh}};
    for (const Vec2 &c : corners)
      if (m.in_bounds(c.x, c.y) && !m.blocked(c.x, c.y, layer, net))
        pts.push_back(c);
  }

  std::vector<Vec2> uniq;
  for (const Vec2 &p : pts) {
    const Vec2 pr{std::round(p.x * 1000.0) / 1000.0, std::round(p.y * 1000.0) / 1000.0};
    bool dup = false;
    for (const Vec2 &u : uniq)
      if (dist(pr, u) < grid_mm * 0.4) {
        dup = true;
        break;
      }
    if (!dup)
      uniq.push_back(pr);
    if (uniq.size() >= 48)
      break;
  }
  return uniq;
}

} // namespace

// ---------------------------------------------------------------------------
// Free-angle route (Python free_angle_route port)
// ---------------------------------------------------------------------------

std::vector<Vec2> free_angle_route_exact(const ExactMap &m, Vec2 start, Vec2 goal, int layer,
                                         int net, double grid_mm, int max_expansions,
                                         double width_mm, const CongestionView *congestion,
                                         std::string *method_out) {
  grid_mm = std::max(0.05, grid_mm > 0 ? grid_mm : 0.1);
  if (max_expansions <= 8000) {
    const double scale = std::max(1.0, 0.35 / grid_mm);
    max_expansions = static_cast<int>(
        std::min(20000.0, std::max(2500.0, max_expansions * scale)));
  }

  auto set_method = [&](const char *s) {
    if (method_out)
      *method_out = s;
  };
  auto seg_blocked = [&](double x1, double y1, double x2, double y2) {
    return m.segment_blocked(x1, y1, x2, y2, layer, net, width_mm);
  };
  auto edge_cost = [&](double x1, double y1, double x2, double y2, double base) {
    if (congestion == nullptr || congestion->empty())
      return base;
    return congestion->edge_cost(x1, y1, x2, y2);
  };
  auto valid_mid = [&](const Vec2 &p) {
    return m.in_bounds(p.x, p.y) && !m.blocked(p.x, p.y, layer, net);
  };

  // 1) Straight free-angle when fully clear. Under negotiated congestion it
  // remains a candidate, not an unconditional winner: PathFinder history must
  // be able to move a net away from an overused but geometrically open lane.
  const bool direct_clear = !seg_blocked(start.x, start.y, goal.x, goal.y);
  if (direct_clear && (congestion == nullptr || congestion->empty())) {
    set_method("los");
    return {start, goal};
  }

  // 2) isotropic detours: obstacle corners + bulges + angled offsets + radar
  const double reach = std::max(40.0, dist(start, goal) * 1.8);
  std::vector<Vec2> detour_pts;
  const double corner_pad = std::max(grid_mm * 2.0, 0.2);
  for (const ExRect &ob : m.rects(layer)) {
    if (ob.net >= 0 && ob.net == net)
      continue;
    if (dist(ob.cx, ob.cy, start.x, start.y) > reach &&
        dist(ob.cx, ob.cy, goal.x, goal.y) > reach)
      continue;
    const double hw = ob.w / 2 + corner_pad, hh = ob.h / 2 + corner_pad;
    detour_pts.push_back({ob.cx - hw, ob.cy - hh});
    detour_pts.push_back({ob.cx + hw, ob.cy - hh});
    detour_pts.push_back({ob.cx - hw, ob.cy + hh});
    detour_pts.push_back({ob.cx + hw, ob.cy + hh});
  }
  const double mx = (start.x + goal.x) / 2, my = (start.y + goal.y) / 2;
  const double dx = goal.x - start.x, dy = goal.y - start.y;
  const double length = std::max(std::hypot(dx, dy), 1e-12);
  const double ux = dx / length, uy = dy / length;
  const double px = -uy, py = ux;
  detour_pts.push_back({start.x, goal.y});
  detour_pts.push_back({goal.x, start.y});
  detour_pts.push_back({mx, my});
  for (double t : {0.2, 0.4, 0.5, 0.6, 0.8}) {
    const double bx = start.x + dx * t, by = start.y + dy * t;
    for (double sign : {1.0, -1.0})
      for (double k : {2.0, 4.0, 7.0, 12.0, 18.0})
        detour_pts.push_back({bx + sign * px * grid_mm * k, by + sign * py * grid_mm * k});
  }
  for (double ang : {M_PI / 6, M_PI / 4, M_PI / 3, -M_PI / 6, -M_PI / 4, -M_PI / 3}) {
    const double ca = std::cos(ang), sa = std::sin(ang);
    const double rx = ux * ca - uy * sa, ry = ux * sa + uy * ca;
    for (double k : {3.0, 6.0, 10.0})
      detour_pts.push_back({mx + rx * grid_mm * k, my + ry * grid_mm * k});
  }
  for (const Vec2 &p :
       radar_scan(m, start, goal, layer, net, 12, std::max(grid_mm, 0.15)))
    detour_pts.push_back(p);

  auto detour_cost = [&](const Vec2 &mid) {
    return edge_cost(start.x, start.y, mid.x, mid.y, dist(start, mid)) +
           edge_cost(mid.x, mid.y, goal.x, goal.y, dist(mid, goal));
  };
  std::vector<std::pair<double, Vec2>> costed;
  costed.reserve(detour_pts.size());
  for (const Vec2 &p : detour_pts)
    costed.push_back({detour_cost(p), p});
  std::stable_sort(costed.begin(), costed.end(),
                   [](const auto &a, const auto &b) { return a.first < b.first; });
  for (size_t i = 0; i < costed.size(); ++i)
    detour_pts[i] = costed[i].second;

  const double direct_cost =
      direct_clear
          ? edge_cost(start.x, start.y, goal.x, goal.y, dist(start, goal))
          : std::numeric_limits<double>::max();
  for (const Vec2 &mid : detour_pts) {
    if (!valid_mid(mid))
      continue;
    if (seg_blocked(start.x, start.y, mid.x, mid.y))
      continue;
    if (seg_blocked(mid.x, mid.y, goal.x, goal.y))
      continue;
    const double candidate_cost = detour_cost(mid);
    if (candidate_cost + 1e-9 < direct_cost) {
      set_method(congestion != nullptr && !congestion->empty()
                     ? "detour_congestion"
                     : "detour");
      return {start, mid, goal};
    }
    break;
  }
  if (direct_clear) {
    set_method("los_congestion");
    return {start, goal};
  }

  // two-corner chain (capped — clearance checks are the cost)
  const size_t lim2 = std::min<size_t>(detour_pts.size(), 28);
  for (size_t i = 0; i < lim2; ++i) {
    const Vec2 &a = detour_pts[i];
    if (!valid_mid(a) || seg_blocked(start.x, start.y, a.x, a.y))
      continue;
    for (size_t j = 0; j < lim2; ++j) {
      if (i == j)
        continue;
      const Vec2 &b = detour_pts[j];
      if (!valid_mid(b))
        continue;
      if (seg_blocked(a.x, a.y, b.x, b.y))
        continue;
      if (seg_blocked(b.x, b.y, goal.x, goal.y))
        continue;
      set_method("detour2");
      return {start, a, b, goal};
    }
  }

  // three-corner chain — small fan only (A* handles dense blocks)
  const size_t lim3 = std::min<size_t>(detour_pts.size(), 12);
  for (size_t ai = 0; ai < lim3; ++ai) {
    const Vec2 &a = detour_pts[ai];
    if (!valid_mid(a) || seg_blocked(start.x, start.y, a.x, a.y))
      continue;
    for (size_t bi = ai + 1; bi < std::min(ai + 6, lim3); ++bi) {
      const Vec2 &b = detour_pts[bi];
      if (!valid_mid(b) || seg_blocked(a.x, a.y, b.x, b.y))
        continue;
      for (size_t ci = 0; ci < std::min<size_t>(detour_pts.size(), 8); ++ci) {
        if (ci == ai || ci == bi)
          continue;
        const Vec2 &c = detour_pts[ci];
        if (!valid_mid(c))
          continue;
        if (seg_blocked(b.x, b.y, c.x, c.y))
          continue;
        if (seg_blocked(c.x, c.y, goal.x, goal.y))
          continue;
        set_method("detour3");
        return {start, a, b, c, goal};
      }
    }
  }

  // 3) hierarchical A*: requested grid, then coarser (16-dir on fine grids)
  static const int DIRS16[16][2] = {{1, 0},  {-1, 0}, {0, 1},  {0, -1}, {1, 1},  {1, -1},
                                    {-1, 1}, {-1, -1}, {2, 1},  {2, -1}, {-2, 1}, {-2, -1},
                                    {1, 2},  {1, -2}, {-1, 2}, {-1, -2}};
  const double span = std::max(dist(start, goal), 1.0);
  std::vector<double> grids_try;
  for (double g : {grid_mm, 0.25, 0.5, 1.0}) {
    g = std::max(0.05, g);
    if (grids_try.empty() || g > grids_try.back() + 1e-9)
      grids_try.push_back(g);
  }

  for (double gcell : grids_try) {
    int budget = static_cast<int>(std::min(
        60000.0, std::max(4000.0, (span / gcell) * 40.0 * std::max(1.0, 0.25 / gcell))));
    budget = std::min(budget, max_expansions);
    const int ndirs = gcell <= 0.3 ? 16 : 8;
    auto snap = [&](double v) { return std::round(v / gcell) * gcell; };
    const double sx = snap(start.x), sy = snap(start.y);
    const double gx = snap(goal.x), gy = snap(goal.y);
    auto cell_key = [&](double x, double y) {
      return (static_cast<int64_t>(std::llround(x / gcell)) << 32) ^
             (static_cast<int64_t>(std::llround(y / gcell)) & 0xffffffffLL);
    };
    const int64_t start_key = cell_key(sx, sy);
    const int64_t goal_key = cell_key(gx, gy);

    using QE = std::tuple<double, double, int64_t>; // f, g, key
    std::priority_queue<QE, std::vector<QE>, std::greater<QE>> open;
    std::unordered_map<int64_t, int64_t> came;
    std::unordered_map<int64_t, double> gscore;
    std::unordered_map<int64_t, Vec2> pos_of;

    open.push({dist(sx, sy, gx, gy), 0.0, start_key});
    came[start_key] = start_key;
    gscore[start_key] = 0.0;
    pos_of[start_key] = {sx, sy};
    int expansions = 0;

    while (!open.empty() && expansions < budget) {
      auto [f, gcost, k] = open.top();
      open.pop();
      ++expansions;
      const Vec2 cp = pos_of[k];
      if (k == goal_key || dist(cp.x, cp.y, gx, gy) <= gcell * 1.6) {
        std::vector<Vec2> path;
        int64_t cur = k;
        for (;;) {
          path.push_back(pos_of[cur]);
          const int64_t prev = came[cur];
          if (prev == cur)
            break;
          cur = prev;
        }
        std::reverse(path.begin(), path.end());
        path.front() = start;
        path.back() = goal;
        set_method("astar");
        // A clearance-only rubberband can collapse the path straight back
        // through an expensive historical lane. Preserve the negotiated A*
        // geometry until the board reaches a conflict-free iteration.
        if (congestion != nullptr && !congestion->empty())
          return path;
        return rubberband_exact(m, path, layer, net, width_mm);
      }

      for (int d = 0; d < ndirs; ++d) {
        const int ddx = DIRS16[d][0], ddy = DIRS16[d][1];
        const double step = gcell * std::hypot(static_cast<double>(ddx), static_cast<double>(ddy));
        const double nx = snap(cp.x + ddx * gcell);
        const double ny = snap(cp.y + ddy * gcell);
        if (!m.in_bounds(nx, ny))
          continue;
        const int64_t nk = cell_key(nx, ny);
        auto git = gscore.find(nk);
        if (git != gscore.end() && git->second <= gcost)
          continue;
        if (seg_blocked(cp.x, cp.y, nx, ny))
          continue;
        const double ng = gcost + edge_cost(cp.x, cp.y, nx, ny, step);
        git = gscore.find(nk);
        if (git == gscore.end() || ng + 1e-9 < git->second) {
          gscore[nk] = ng;
          came[nk] = k;
          pos_of[nk] = {nx, ny};
          open.push({ng + dist(nx, ny, gx, gy), ng, nk});
        }
      }
    }
  }

  return {};
}

} // namespace pr
