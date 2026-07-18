#include "pr_native/router.hpp"
#include "pr_native/gpu.hpp"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <queue>
#include <sstream>
#include <unordered_map>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

namespace pr {
namespace {

inline double dist(Vec2 a, Vec2 b) { return std::hypot(a.x - b.x, a.y - b.y); }

struct NodeKey {
  int x, y;
  bool operator==(const NodeKey &o) const { return x == o.x && y == o.y; }
};
struct NodeHash {
  size_t operator()(const NodeKey &k) const {
    return (static_cast<size_t>(k.x) * 73856093u) ^ (static_cast<size_t>(k.y) * 19349663u);
  }
};

std::vector<Vec2> rubberband(const std::vector<Vec2> &path, const GridMap &g, int layer,
                             int net_id) {
  if (path.size() <= 2)
    return path;
  std::vector<Vec2> out;
  out.push_back(path[0]);
  size_t i = 0;
  while (i + 1 < path.size()) {
    size_t j = path.size() - 1;
    bool advanced = false;
    while (j > i + 1) {
      if (!g.segment_blocked(out.back().x, out.back().y, path[j].x, path[j].y, layer,
                             net_id)) {
        out.push_back(path[j]);
        i = j;
        advanced = true;
        break;
      }
      --j;
    }
    if (!advanced) {
      ++i;
      if (i < path.size() && !(out.back().x == path[i].x && out.back().y == path[i].y))
        out.push_back(path[i]);
    }
  }
  if (!(out.back().x == path.back().x && out.back().y == path.back().y))
    out.push_back(path.back());
  return out;
}

void paint_poly(GridMap &grid, const std::vector<Vec2> &poly, int layer, int net_id,
                double width, double clearance) {
  // Grid queries operate on centerlines. Reserve both track half-widths plus
  // the required edge clearance (equal-width assumption for the fast map).
  const double centerline_keepout_diameter = 2.0 * width + 2.0 * clearance;
  for (size_t i = 0; i + 1 < poly.size(); ++i)
    grid.paint_trace(poly[i].x, poly[i].y, poly[i + 1].x, poly[i + 1].y,
                     centerline_keepout_diameter, layer, net_id);
}

void emit_poly(const std::vector<Vec2> &poly, int layer, int net_id, double width,
               std::vector<Segment> &out_segs) {
  for (size_t i = 0; i + 1 < poly.size(); ++i) {
    Segment s;
    s.x1 = poly[i].x;
    s.y1 = poly[i].y;
    s.x2 = poly[i + 1].x;
    s.y2 = poly[i + 1].y;
    s.layer = layer;
    s.net_id = net_id;
    s.width_mm = width;
    out_segs.push_back(s);
  }
}

double cross(Vec2 origin, Vec2 a, Vec2 b) {
  return (a.x - origin.x) * (b.y - origin.y) -
         (a.y - origin.y) * (b.x - origin.x);
}

std::vector<Vec2> convex_hull(std::vector<Vec2> points) {
  if (points.size() < 3)
    return points;
  std::sort(points.begin(), points.end(), [](Vec2 a, Vec2 b) {
    return a.x < b.x || (a.x == b.x && a.y < b.y);
  });
  points.erase(std::unique(points.begin(), points.end(), [](Vec2 a, Vec2 b) {
                 return std::abs(a.x - b.x) < 1e-9 &&
                        std::abs(a.y - b.y) < 1e-9;
               }),
               points.end());
  if (points.size() < 3)
    return points;
  std::vector<Vec2> hull(points.size() * 2);
  size_t k = 0;
  for (const auto &point : points) {
    while (k >= 2 && cross(hull[k - 2], hull[k - 1], point) <= 0)
      --k;
    hull[k++] = point;
  }
  for (size_t i = points.size() - 1, lower = k + 1; i > 0; --i) {
    const auto &point = points[i - 1];
    while (k >= lower && cross(hull[k - 2], hull[k - 1], point) <= 0)
      --k;
    hull[k++] = point;
  }
  hull.resize(k - 1);
  return hull;
}

bool point_in_polygon(Vec2 point, const std::vector<Vec2> &polygon) {
  bool inside = false;
  if (polygon.size() < 3)
    return true;
  size_t previous = polygon.size() - 1;
  for (size_t current = 0; current < polygon.size(); ++current) {
    const auto &a = polygon[current];
    const auto &b = polygon[previous];
    if (((a.y > point.y) != (b.y > point.y)) &&
        point.x < (b.x - a.x) * (point.y - a.y) /
                          ((b.y - a.y) + 1e-18) +
                      a.x)
      inside = !inside;
    previous = current;
  }
  return inside;
}

std::vector<Vec2> organic_area(const std::vector<Vec2> &anchors,
                               double margin, const RouteConfig &cfg) {
  if (anchors.empty())
    return {};
  // Convex hull of small circles is a rounded Minkowski expansion of the
  // anchor hull: a stable organic boundary without a polygon clipping stack.
  constexpr int circle_steps = 16;
  std::vector<Vec2> cloud;
  cloud.reserve(anchors.size() * circle_steps);
  const double radius = std::max(margin, cfg.clearance_mm * 2.0);
  for (const auto &anchor : anchors) {
    for (int i = 0; i < circle_steps; ++i) {
      double angle = 2.0 * M_PI * static_cast<double>(i) / circle_steps;
      cloud.push_back(
          {std::clamp(anchor.x + radius * std::cos(angle), cfg.x_min, cfg.x_max),
           std::clamp(anchor.y + radius * std::sin(angle), cfg.y_min, cfg.y_max)});
    }
  }
  auto hull = convex_hull(std::move(cloud));
  if (cfg.board_outline.size() < 3)
    return hull;

  Vec2 center{};
  for (const auto &anchor : anchors) {
    center.x += anchor.x;
    center.y += anchor.y;
  }
  center.x /= static_cast<double>(anchors.size());
  center.y /= static_cast<double>(anchors.size());
  if (!point_in_polygon(center, cfg.board_outline))
    return {};

  // Project rounded boundary samples back inside a non-rectangular Edge.Cuts
  // polygon (HALO's teardrop is the motivating case).
  for (auto &point : hull) {
    if (point_in_polygon(point, cfg.board_outline))
      continue;
    Vec2 low = center;
    Vec2 high = point;
    for (int iteration = 0; iteration < 32; ++iteration) {
      Vec2 mid{(low.x + high.x) * 0.5, (low.y + high.y) * 0.5};
      if (point_in_polygon(mid, cfg.board_outline))
        low = mid;
      else
        high = mid;
    }
    point = low;
  }
  return hull;
}

} // namespace

const char *native_version() { return "1.6.0-native-bundle"; }

std::vector<Vec2> rubberband_path(const std::vector<Vec2> &path, const GridMap &g, int layer,
                                  int net_id) {
  return rubberband(path, g, layer, net_id);
}

std::vector<Vec2> route_point(const GridMap &grid, Vec2 start, Vec2 goal, int layer, int net_id,
                              int max_expansions, bool isotropic) {
  // 1) LOS free-angle
  if (!grid.segment_blocked(start.x, start.y, goal.x, goal.y, layer, net_id))
    return {start, goal};

  const double gmm = grid.grid();
  double dx = goal.x - start.x, dy = goal.y - start.y;
  double length = std::hypot(dx, dy);
  if (length < 1e-12)
    return {start, goal};
  double ux = dx / length, uy = dy / length;
  double px = -uy, py = ux; // perpendicular

  // 2) Isotropic detours: perpendicular bulges + angled offsets (TopoR-style)
  std::vector<Vec2> mids;
  if (isotropic) {
    for (double t : {0.2, 0.4, 0.5, 0.6, 0.8}) {
      double bx = start.x + dx * t, by = start.y + dy * t;
      for (double sign : {1.0, -1.0}) {
        for (double k : {2.0, 4.0, 7.0, 12.0, 18.0}) {
          mids.push_back({bx + sign * px * gmm * k, by + sign * py * gmm * k});
        }
      }
    }
    for (double ang : {M_PI / 6, M_PI / 4, M_PI / 3, -M_PI / 6, -M_PI / 4, -M_PI / 3}) {
      double ca = std::cos(ang), sa = std::sin(ang);
      double rx = ux * ca - uy * sa, ry = ux * sa + uy * ca;
      double mx = (start.x + goal.x) * 0.5, my = (start.y + goal.y) * 0.5;
      for (double k : {3.0, 6.0, 10.0})
        mids.push_back({mx + rx * gmm * k, my + ry * gmm * k});
    }
  }
  // Classic L-bends + midpoint
  mids.push_back({start.x, goal.y});
  mids.push_back({goal.x, start.y});
  mids.push_back({(start.x + goal.x) * 0.5, (start.y + goal.y) * 0.5});

  // Prefer shorter detours
  std::sort(mids.begin(), mids.end(), [&](const Vec2 &a, const Vec2 &b) {
    return dist(start, a) + dist(a, goal) < dist(start, b) + dist(b, goal);
  });

  for (const auto &m : mids) {
    if (!grid.in_bounds(m.x, m.y) || grid.point_blocked(m.x, m.y, layer, net_id))
      continue;
    if (!grid.segment_blocked(start.x, start.y, m.x, m.y, layer, net_id) &&
        !grid.segment_blocked(m.x, m.y, goal.x, goal.y, layer, net_id))
      return {start, m, goal};
  }

  // Two-corner isotropic chain (limited; matches Python cand cap)
  size_t lim = std::min<size_t>(mids.size(), 28);
  for (size_t i = 0; i < lim; ++i) {
    const auto &a = mids[i];
    if (!grid.in_bounds(a.x, a.y) || grid.point_blocked(a.x, a.y, layer, net_id))
      continue;
    if (grid.segment_blocked(start.x, start.y, a.x, a.y, layer, net_id))
      continue;
    for (size_t j = 0; j < lim; ++j) {
      if (i == j)
        continue;
      const auto &b = mids[j];
      if (!grid.in_bounds(b.x, b.y) || grid.point_blocked(b.x, b.y, layer, net_id))
        continue;
      if (grid.segment_blocked(a.x, a.y, b.x, b.y, layer, net_id))
        continue;
      if (grid.segment_blocked(b.x, b.y, goal.x, goal.y, layer, net_id))
        continue;
      return {start, a, b, goal};
    }
  }

  // 3) Grid A* (8-connected)
  auto snap = [&](Vec2 p) -> NodeKey {
    return {grid.world_to_ix(p.x), grid.world_to_iy(p.y)};
  };
  NodeKey sk = snap(start), gk = snap(goal);
  using QE = std::pair<double, NodeKey>;
  auto cmp = [](const QE &a, const QE &b) { return a.first > b.first; };
  std::priority_queue<QE, std::vector<QE>, decltype(cmp)> open(cmp);
  std::unordered_map<NodeKey, NodeKey, NodeHash> came;
  std::unordered_map<NodeKey, double, NodeHash> gscore;
  std::unordered_map<NodeKey, Vec2, NodeHash> pos;

  auto h = [&](NodeKey k) {
    return dist({grid.ix_to_world(k.x), grid.iy_to_world(k.y)},
                {grid.ix_to_world(gk.x), grid.iy_to_world(gk.y)});
  };

  open.push({h(sk), sk});
  gscore[sk] = 0;
  came[sk] = sk;
  pos[sk] = start;

  static const int DX[8] = {1, -1, 0, 0, 1, 1, -1, -1};
  static const int DY[8] = {0, 0, 1, -1, 1, -1, 1, -1};

  int expansions = 0;
  bool found = false;
  NodeKey end = sk;

  while (!open.empty() && expansions < max_expansions) {
    auto [f, cur] = open.top();
    open.pop();
    ++expansions;
    Vec2 cp = pos[cur];
    if ((cur.x == gk.x && cur.y == gk.y) || dist(cp, goal) <= gmm * 1.5) {
      found = true;
      end = cur;
      break;
    }
    double gc = gscore[cur];
    for (int d = 0; d < 8; ++d) {
      int nix = cur.x + DX[d], niy = cur.y + DY[d];
      if (grid.cell_blocked(nix, niy, layer, net_id))
        continue;
      Vec2 np{grid.ix_to_world(nix), grid.iy_to_world(niy)};
      if (grid.segment_blocked(cp.x, cp.y, np.x, np.y, layer, net_id))
        continue;
      NodeKey nk{nix, niy};
      double step = gmm * ((DX[d] && DY[d]) ? 1.414213562 : 1.0);
      double ng = gc + step;
      auto it = gscore.find(nk);
      if (it != gscore.end() && it->second <= ng + 1e-12)
        continue;
      gscore[nk] = ng;
      came[nk] = cur;
      pos[nk] = np;
      open.push({ng + h(nk), nk});
    }
  }

  if (!found)
    return {};

  std::vector<Vec2> path;
  NodeKey cur = end;
  for (;;) {
    path.push_back(pos[cur]);
    if (cur.x == sk.x && cur.y == sk.y)
      break;
    cur = came[cur];
  }
  std::reverse(path.begin(), path.end());
  path.front() = start;
  path.back() = goal;
  return rubberband(path, grid, layer, net_id);
}

static bool route_edge(GridMap &grid, const RouteConfig &cfg, Vec2 a, Vec2 b, int net_id,
                       const std::vector<int> &layers, double width,
                       std::vector<Segment> &out_segs, std::vector<Via> &out_vias,
                       std::string &method) {
  std::vector<int> blocked_layers;
  int alts = 0;

  // try each preferred layer (isotropic free-angle)
  for (int layer : layers) {
    auto poly = route_point(grid, a, b, layer, net_id, cfg.max_expansions, cfg.isotropic);
    ++alts;
    if (poly.size() >= 2) {
      method = cfg.isotropic ? "isotropic" : "astar";
      emit_poly(poly, layer, net_id, width, out_segs);
      paint_poly(grid, poly, layer, net_id, width, cfg.clearance_mm);
      return true;
    }
    blocked_layers.push_back(layer);
  }
  if (!cfg.allow_vias || layers.size() < 2)
    return false;

  // Multi-site via search — dense sites: connectivity beats via-count purity
  double g = std::max(cfg.grid_mm, 0.15);
  Vec2 mid{(a.x + b.x) * 0.5, (a.y + b.y) * 0.5};
  std::vector<Vec2> sites = {
      mid,
      {a.x, b.y},
      {b.x, a.y},
      {(2 * a.x + b.x) / 3, (2 * a.y + b.y) / 3},
      {(a.x + 2 * b.x) / 3, (a.y + 2 * b.y) / 3},
  };
  for (double k : {2.0, 4.0, 7.0, 11.0}) {
    sites.push_back({mid.x + k * g, mid.y});
    sites.push_back({mid.x - k * g, mid.y});
    sites.push_back({mid.x, mid.y + k * g});
    sites.push_back({mid.x, mid.y - k * g});
    sites.push_back({mid.x + k * g, mid.y + k * g});
    sites.push_back({mid.x - k * g, mid.y - k * g});
  }
  for (double k : {2.0, 5.0}) {
    sites.push_back({a.x + k * g, a.y});
    sites.push_back({a.x, a.y + k * g});
    sites.push_back({b.x - k * g, b.y});
    sites.push_back({b.x, b.y - k * g});
  }

  // Prefer primary layer to each other preferred layer (matrix/CPX striping)
  std::vector<std::pair<int, int>> layer_pairs;
  if (layers.size() >= 2) {
    for (size_t j = 1; j < layers.size(); ++j) {
      layer_pairs.push_back({layers[0], layers[j]});
      layer_pairs.push_back({layers[j], layers[0]});
    }
    if (layers.size() > 2) {
      layer_pairs.push_back({layers[1], layers.back()});
    }
  }
  int sites_tried = 0;
  int exp_budget = std::max(500, cfg.max_expansions / 2);
  for (const auto &lp : layer_pairs) {
    int l0 = lp.first, l1 = lp.second;
    for (const auto &site : sites) {
      double vx = std::round(site.x / g) * g;
      double vy = std::round(site.y / g) * g;
      ++sites_tried;
      if (sites_tried > 80) // cap for speed on dense boards
        break;
      if (!grid.in_bounds(vx, vy))
        continue;
      if (grid.point_blocked(vx, vy, l0, net_id) || grid.point_blocked(vx, vy, l1, net_id))
        continue;
      Vec2 via_pt{vx, vy};
      auto p0 = route_point(grid, a, via_pt, l0, net_id, exp_budget, cfg.isotropic);
      auto p1 = route_point(grid, via_pt, b, l1, net_id, exp_budget, cfg.isotropic);
      if (p0.size() >= 2 && p1.size() >= 2) {
        method = "via";
        emit_poly(p0, l0, net_id, width, out_segs);
        paint_poly(grid, p0, l0, net_id, width, cfg.clearance_mm);
        emit_poly(p1, l1, net_id, width, out_segs);
        paint_poly(grid, p1, l1, net_id, width, cfg.clearance_mm);
        Via v;
        v.x = vx;
        v.y = vy;
        v.net_id = net_id;
        v.layer_a = l0;
        v.layer_b = l1;
        v.alternatives_considered = alts + sites_tried;
        {
          std::ostringstream oss;
          oss << "Same-layer blocked; via L" << l0 << "->L" << l1 << " @(" << vx << "," << vy
              << "). Tried " << alts << " same-layer + " << sites_tried << " via sites.";
          v.reason = oss.str();
        }
        out_vias.push_back(v);
        return true;
      }
    }
  }
  return false;
}

int remove_redundant_vias(RouteResult &r, GridMap &grid, const RouteConfig &cfg) {
  if (r.vias.empty())
    return 0;
  int removed = 0;
  std::vector<Via> kept;
  std::vector<Segment> segs = r.segments;

  for (const auto &via : r.vias) {
    const double tol = 0.4;
    std::vector<size_t> incident;
    for (size_t i = 0; i < segs.size(); ++i) {
      const auto &s = segs[i];
      if (s.net_id != via.net_id)
        continue;
      if (dist({s.x1, s.y1}, {via.x, via.y}) < tol || dist({s.x2, s.y2}, {via.x, via.y}) < tol)
        incident.push_back(i);
    }
    if (incident.size() < 2) {
      kept.push_back(via);
      continue;
    }
    std::vector<Vec2> ends;
    double width = segs[incident[0]].width_mm;
    for (size_t ii : incident) {
      const auto &s = segs[ii];
      if (dist({s.x1, s.y1}, {via.x, via.y}) < tol)
        ends.push_back({s.x2, s.y2});
      else
        ends.push_back({s.x1, s.y1});
    }
    bool merged = false;
    for (int ly = 0; ly < cfg.num_layers; ++ly) {
      bool ok = true;
      for (const auto &e : ends) {
        if (grid.segment_blocked(e.x, e.y, via.x, via.y, ly, via.net_id)) {
          ok = false;
          break;
        }
      }
      if (!ok)
        continue;
      // remove incident segments (high indices first)
      std::sort(incident.rbegin(), incident.rend());
      for (size_t ii : incident)
        segs.erase(segs.begin() + static_cast<long>(ii));
      for (const auto &e : ends) {
        Segment s{e.x, e.y, via.x, via.y, ly, via.net_id, width};
        segs.push_back(s);
        grid.paint_trace(s.x1, s.y1, s.x2, s.y2, width + cfg.clearance_mm, ly, via.net_id);
      }
      ++removed;
      merged = true;
      break;
    }
    if (!merged)
      kept.push_back(via);
  }

  if (removed == 0)
    return 0;
  r.segments = std::move(segs);
  r.vias = std::move(kept);
  r.via_count = static_cast<int>(r.vias.size());
  r.total_length_mm = 0;
  for (const auto &s : r.segments)
    r.total_length_mm += dist({s.x1, s.y1}, {s.x2, s.y2});
  r.notes.push_back("via_minimize: removed " + std::to_string(removed) + " via(s)");
  return removed;
}

static void post_rubberband(RouteResult &r, GridMap &grid) {
  // Rebuild continuous chains per net/layer and rubberband. Multipin nets are
  // spanning trees, not polylines: greedily chaining through a branch can
  // collapse one arm and silently disconnect an anchor. Preserve those trees
  // until a branch-aware graph rubberband is available.
  std::vector<Segment> out;
  std::unordered_map<int, bool> preserve_tree;
  for (const auto &report : r.net_reports)
    preserve_tree[report.net_id] = report.pins > 2;
  int preserved_trees = 0;
  // Group by net+layer
  std::unordered_map<long long, std::vector<Segment>> groups;
  for (const auto &s : r.segments) {
    long long key = (static_cast<long long>(s.net_id) << 16) | s.layer;
    groups[key].push_back(s);
  }
  double total = 0;
  for (auto &kv : groups) {
    auto &segs = kv.second;
    if (segs.empty())
      continue;
    const int group_net = segs.front().net_id;
    if (preserve_tree[group_net]) {
      out.insert(out.end(), segs.begin(), segs.end());
      for (const auto &segment : segs)
        total += dist({segment.x1, segment.y1}, {segment.x2, segment.y2});
      ++preserved_trees;
      continue;
    }
    // Build polylines by chaining endpoints
    std::vector<char> used(segs.size(), 0);
    for (size_t seed = 0; seed < segs.size(); ++seed) {
      if (used[seed])
        continue;
      std::vector<Vec2> pts = {{segs[seed].x1, segs[seed].y1}, {segs[seed].x2, segs[seed].y2}};
      used[seed] = 1;
      bool grew = true;
      while (grew) {
        grew = false;
        for (size_t i = 0; i < segs.size(); ++i) {
          if (used[i])
            continue;
          Vec2 a{segs[i].x1, segs[i].y1}, b{segs[i].x2, segs[i].y2};
          if (dist(pts.back(), a) < 0.08) {
            pts.push_back(b);
            used[i] = 1;
            grew = true;
          } else if (dist(pts.back(), b) < 0.08) {
            pts.push_back(a);
            used[i] = 1;
            grew = true;
          } else if (dist(pts.front(), a) < 0.08) {
            pts.insert(pts.begin(), b);
            used[i] = 1;
            grew = true;
          } else if (dist(pts.front(), b) < 0.08) {
            pts.insert(pts.begin(), a);
            used[i] = 1;
            grew = true;
          }
        }
      }
      int layer = segs[seed].layer;
      int net = segs[seed].net_id;
      double w = segs[seed].width_mm;
      auto cleaned = rubberband(pts, grid, layer, net);
      for (size_t i = 0; i + 1 < cleaned.size(); ++i) {
        Segment s{cleaned[i].x, cleaned[i].y, cleaned[i + 1].x, cleaned[i + 1].y, layer, net, w};
        out.push_back(s);
        total += dist(cleaned[i], cleaned[i + 1]);
      }
    }
  }
  if (!out.empty()) {
    size_t before = r.segments.size();
    r.segments = std::move(out);
    r.total_length_mm = total;
    r.notes.push_back("post_rubberband: segs " + std::to_string(before) + "→" +
                      std::to_string(r.segments.size()));
    if (preserved_trees > 0)
      r.notes.push_back("post_rubberband: preserved " +
                        std::to_string(preserved_trees) +
                        " multipin tree layer(s)");
  }
}

void compute_quality(RouteResult &r) {
  int n = std::max(1, static_cast<int>(r.net_reports.size()));
  int routed = 0;
  for (const auto &nr : r.net_reports)
    if (nr.status == "ok" || nr.status == "soft_violation")
      ++routed;
  double completion = static_cast<double>(routed) / n;
  double viol_pen = std::min(40.0, r.clearance_violations * 4.0);
  double via_pen = std::min(20.0, r.via_count * 0.8);
  double un_pen = std::min(40.0, static_cast<double>(r.unrouted.size()) * 8.0);
  r.quality_score = std::max(0.0, 100.0 * completion - viol_pen - via_pen - un_pen);
  if (r.quality_score >= 90)
    r.quality_grade = "A";
  else if (r.quality_score >= 75)
    r.quality_grade = "B";
  else if (r.quality_score >= 55)
    r.quality_grade = "C";
  else if (r.quality_score >= 35)
    r.quality_grade = "D";
  else
    r.quality_grade = "F";
}

RouteResult route_board(const std::vector<NetSpec> &nets, const RouteConfig &cfg,
                        const std::vector<RectObs> &pad_obstacles, ProgressFn progress) {
  using clock = std::chrono::steady_clock;
  auto t0 = clock::now();

  RouteResult result;
  result.used_native = true;

  auto gpu = gpu_probe();
  result.used_gpu = gpu.available && cfg.use_gpu;
  if (result.used_gpu)
    result.notes.push_back("gpu: " + gpu.backend + " " + gpu.device_name);
  else
    result.notes.push_back("gpu: unavailable — OpenMP/serial CPU");

#ifdef PR_HAS_OPENMP
  result.notes.push_back("openmp: enabled");
#else
  result.notes.push_back("openmp: disabled");
#endif
  result.notes.push_back(std::string("style: ") + (cfg.isotropic ? "isotropic" : "grid"));
  result.notes.push_back(std::string("native_version: ") + native_version());

  GridMap grid(cfg.x_min, cfg.x_max, cfg.y_min, cfg.y_max, cfg.grid_mm, cfg.num_layers);

  if (cfg.board_outline.size() >= 3) {
    auto &cells = grid.data();
    for (int layer = 0; layer < grid.layers(); ++layer) {
      for (int iy = 0; iy < grid.height(); ++iy) {
        for (int ix = 0; ix < grid.width(); ++ix) {
          Vec2 point{grid.ix_to_world(ix), grid.iy_to_world(iy)};
          if (!point_in_polygon(point, cfg.board_outline)) {
            size_t index = (static_cast<size_t>(layer) * grid.height() + iy) *
                               grid.width() +
                           ix;
            cells[index] = 255;
          }
        }
      }
    }
  }

  // paint pads
  for (const auto &ob : pad_obstacles) {
    std::vector<int> obstacle_layers = ob.layers;
    if (obstacle_layers.empty()) {
      for (int ly = 0; ly < cfg.num_layers; ++ly)
        obstacle_layers.push_back(ly);
    }
    for (int ly : obstacle_layers) {
      if (ly < 0 || ly >= cfg.num_layers)
        continue;
      grid.paint_rect(ob.cx, ob.cy, ob.w + cfg.clearance_mm * 2 + 0.25,
                      ob.h + cfg.clearance_mm * 2 + 0.25, ly, ob.net_id);
    }
  }

  // sort nets by priority desc, then fewer pins first
  std::vector<int> order(nets.size());
  for (size_t i = 0; i < nets.size(); ++i)
    order[i] = static_cast<int>(i);
  std::stable_sort(order.begin(), order.end(), [&](int a, int b) {
    if (nets[a].priority != nets[b].priority)
      return nets[a].priority > nets[b].priority;
    return nets[a].anchors.size() < nets[b].anchors.size();
  });

  int total = static_cast<int>(order.size());
  int done = 0;

  for (int oi : order) {
    const auto &net = nets[oi];
    NetReport rep;
    rep.net_id = net.net_id;
    rep.name = net.name;
    rep.pins = static_cast<int>(net.anchors.size());

    if (progress)
      progress(done, total, net.name, "routing");

    if (net.anchors.size() < 2) {
      rep.status = "skipped";
      result.net_reports.push_back(rep);
      ++done;
      continue;
    }

    if (net.use_copper_area) {
      CopperArea area;
      area.outline = organic_area(net.anchors, net.area_margin_mm, cfg);
      area.layer = net.area_layer >= 0
                       ? net.area_layer
                       : (net.preferred_layers.empty()
                              ? 0
                              : net.preferred_layers.front());
      area.layer = std::clamp(area.layer, 0, cfg.num_layers - 1);
      area.net_id = net.net_id;
      area.clearance_mm = cfg.clearance_mm;
      area.min_thickness_mm = std::max(0.1, net.width_mm * 0.5);
      area.priority = net.area_priority;
      if (area.outline.size() >= 3) {
        result.areas.push_back(std::move(area));
        rep.status = "ok";
        rep.method = "copper_area";
      } else {
        rep.status = "unrouted";
        rep.method = "area_failed";
        result.unrouted.push_back(net.name);
      }
      result.net_reports.push_back(rep);
      ++done;
      if (progress)
        progress(done, total, net.name, rep.status);
      continue;
    }

    std::vector<int> layers = net.preferred_layers;
    if (layers.empty()) {
      for (int i = 0; i < cfg.num_layers; ++i)
        layers.push_back(i);
    }

    // Prim-style tree on anchors.  Euclidean-nearest is only a preference:
    // try every frontier edge until one can be legally geometrized.  An
    // anchor is never admitted to the tree before copper reaches it.
    const size_t n = net.anchors.size();
    std::vector<char> in(n, 0);
    in[0] = 1;
    size_t in_count = 1;
    int open_edges = 0;
    std::string last_method;
    std::vector<Segment> net_segments;
    std::vector<Via> net_vias;
    GridMap net_grid = grid;

    while (in_count < n) {
      struct Candidate {
        double distance;
        size_t from;
        size_t to;
      };
      std::vector<Candidate> candidates;
      for (size_t i = 0; i < n; ++i)
        if (in[i])
          for (size_t j = 0; j < n; ++j)
            if (!in[j])
              candidates.push_back(
                  {dist(net.anchors[i], net.anchors[j]), i, j});
      std::sort(candidates.begin(), candidates.end(),
                [](const Candidate &a, const Candidate &b) {
                  return a.distance < b.distance;
                });

      bool connected = false;
      for (const auto &candidate : candidates) {
        std::vector<Segment> edge_segments;
        std::vector<Via> edge_vias;
        std::string method;
        bool ok = route_edge(net_grid, cfg, net.anchors[candidate.from],
                             net.anchors[candidate.to], net.net_id, layers,
                             net.width_mm, edge_segments, edge_vias, method);
        if (!ok)
          continue;
        in[candidate.to] = 1;
        ++in_count;
        connected = true;
        last_method = method;
        net_segments.insert(net_segments.end(), edge_segments.begin(),
                            edge_segments.end());
        for (const auto &v : edge_vias) {
            net_vias.push_back(v);
            for (int ly = 0; ly < cfg.num_layers; ++ly)
              net_grid.paint_rect(
                  v.x, v.y,
                  v.size_mm + 2.0 * cfg.clearance_mm + net.width_mm,
                  v.size_mm + 2.0 * cfg.clearance_mm + net.width_mm, ly,
                  net.net_id);
        }
        break;
      }

      if (connected)
        continue;

      if (cfg.soft_fallback && !candidates.empty()) {
        const auto &candidate = candidates.front();
        const auto &a = net.anchors[candidate.from];
        const auto &b = net.anchors[candidate.to];
        Segment s{a.x, a.y, b.x, b.y, layers.front(), net.net_id,
                  net.width_mm};
        net_segments.push_back(s);
        net_grid.paint_trace(s.x1, s.y1, s.x2, s.y2,
                             s.width_mm + cfg.clearance_mm, s.layer, s.net_id);
        result.clearance_violations++;
        in[candidate.to] = 1;
        ++in_count;
        last_method = "straight_fallback";
        continue;
      }

      open_edges = static_cast<int>(n - in_count);
      last_method = "unrouted_edge";
      break;
    }

    rep.method = last_method;
    const bool complete = in_count == n;
    const bool commit = complete || !cfg.atomic_nets || cfg.soft_fallback;
    if (commit) {
      grid = std::move(net_grid);
      for (const auto &s : net_segments) {
        result.segments.push_back(s);
        double length = dist({s.x1, s.y1}, {s.x2, s.y2});
        result.total_length_mm += length;
        rep.length_mm += length;
        rep.segments++;
      }
      result.vias.insert(result.vias.end(), net_vias.begin(), net_vias.end());
      result.via_count += static_cast<int>(net_vias.size());
      rep.vias = static_cast<int>(net_vias.size());
    }

    if (!complete && (!commit || rep.segments == 0)) {
      rep.status = "unrouted";
      result.unrouted.push_back(net.name);
      if (cfg.atomic_nets && !cfg.soft_fallback)
        rep.method = "atomic_unrouted";
    } else if (!complete || open_edges)
      rep.status = "partial";
    else if (last_method == "straight_fallback")
      rep.status = "soft_violation";
    else
      rep.status = "ok";

    result.net_reports.push_back(rep);
    ++done;
    if (progress)
      progress(done, total, net.name, rep.status);
  }

  // Post: rubberband + via minimize (geometry polish)
  if (cfg.post_rubberband && !result.segments.empty())
    post_rubberband(result, grid);
  if (cfg.via_minimize && !result.vias.empty())
    remove_redundant_vias(result, grid, cfg);

  // Optional GPU batch re-check of a sample of segments
  if (result.used_gpu && !result.segments.empty()) {
    std::vector<double> flat;
    flat.reserve(result.segments.size() * 4);
    size_t ncheck = std::min<size_t>(result.segments.size(), 256);
    for (size_t i = 0; i < ncheck; ++i) {
      const auto &s = result.segments[i];
      flat.push_back(s.x1);
      flat.push_back(s.y1);
      flat.push_back(s.x2);
      flat.push_back(s.y2);
    }
    auto flags = batch_segment_clearance(grid, 0, 0, flat, true);
    result.notes.push_back("gpu_batch_clearance_samples=" + std::to_string(flags.size()));
  }

  // Refresh per-net lengths after polish
  for (auto &nr : result.net_reports) {
    nr.length_mm = 0;
    nr.segments = 0;
    nr.vias = 0;
  }
  std::unordered_map<int, size_t> rep_idx;
  for (size_t i = 0; i < result.net_reports.size(); ++i)
    rep_idx[result.net_reports[i].net_id] = i;
  for (const auto &s : result.segments) {
    auto it = rep_idx.find(s.net_id);
    if (it == rep_idx.end())
      continue;
    auto &nr = result.net_reports[it->second];
    nr.length_mm += dist({s.x1, s.y1}, {s.x2, s.y2});
    nr.segments++;
  }
  for (const auto &v : result.vias) {
    auto it = rep_idx.find(v.net_id);
    if (it != rep_idx.end())
      result.net_reports[it->second].vias++;
  }

  compute_quality(result);
  result.notes.push_back("grade " + result.quality_grade + " (" +
                         std::to_string(static_cast<int>(result.quality_score)) + "/100)");
  auto t1 = clock::now();
  result.elapsed_ms =
      std::chrono::duration<double, std::milli>(t1 - t0).count();
  result.notes.push_back("native_elapsed_ms=" + std::to_string(result.elapsed_ms));
  return result;
}

} // namespace pr
