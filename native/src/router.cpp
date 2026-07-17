#include "pr_native/router.hpp"
#include "pr_native/gpu.hpp"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <queue>
#include <unordered_map>

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

} // namespace

std::vector<Vec2> route_point(const GridMap &grid, Vec2 start, Vec2 goal, int layer,
                              int net_id, int max_expansions) {
  // 1) LOS
  if (!grid.segment_blocked(start.x, start.y, goal.x, goal.y, layer, net_id))
    return {start, goal};

  // 2) L-bends
  Vec2 mids[] = {{start.x, goal.y},
                 {goal.x, start.y},
                 {(start.x + goal.x) * 0.5, (start.y + goal.y) * 0.5}};
  for (const auto &m : mids) {
    if (!grid.in_bounds(m.x, m.y) || grid.point_blocked(m.x, m.y, layer, net_id))
      continue;
    if (!grid.segment_blocked(start.x, start.y, m.x, m.y, layer, net_id) &&
        !grid.segment_blocked(m.x, m.y, goal.x, goal.y, layer, net_id))
      return {start, m, goal};
  }

  // 3) Grid A*
  const double gmm = grid.grid();
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
    if (cur.x == gk.x && cur.y == gk.y || dist(cp, goal) <= gmm * 1.5) {
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
  // try each preferred layer
  for (int layer : layers) {
    auto poly = route_point(grid, a, b, layer, net_id, cfg.max_expansions);
    if (poly.size() >= 2) {
      method = "astar";
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
        if (!cfg.soft_fallback || true)
          grid.paint_trace(s.x1, s.y1, s.x2, s.y2, width + cfg.clearance_mm, layer, net_id);
      }
      return true;
    }
  }
  if (!cfg.allow_vias || layers.size() < 2)
    return false;

  // simple via at midpoint
  Vec2 mid{(a.x + b.x) * 0.5, (a.y + b.y) * 0.5};
  int l0 = layers.front(), l1 = layers.back();
  auto p0 = route_point(grid, a, mid, l0, net_id, cfg.max_expansions / 2);
  auto p1 = route_point(grid, mid, b, l1, net_id, cfg.max_expansions / 2);
  if (p0.size() >= 2 && p1.size() >= 2) {
    method = "via";
    for (size_t i = 0; i + 1 < p0.size(); ++i) {
      Segment s{p0[i].x, p0[i].y, p0[i + 1].x, p0[i + 1].y, l0, net_id, width};
      out_segs.push_back(s);
      grid.paint_trace(s.x1, s.y1, s.x2, s.y2, width + cfg.clearance_mm, l0, net_id);
    }
    for (size_t i = 0; i + 1 < p1.size(); ++i) {
      Segment s{p1[i].x, p1[i].y, p1[i + 1].x, p1[i + 1].y, l1, net_id, width};
      out_segs.push_back(s);
      grid.paint_trace(s.x1, s.y1, s.x2, s.y2, width + cfg.clearance_mm, l1, net_id);
    }
    Via v;
    v.x = mid.x;
    v.y = mid.y;
    v.net_id = net_id;
    v.layer_a = l0;
    v.layer_b = l1;
    out_vias.push_back(v);
    return true;
  }
  return false;
}

void compute_quality(RouteResult &r) {
  int n = std::max(1, static_cast<int>(r.net_reports.size()));
  int routed = 0;
  for (const auto &nr : r.net_reports)
    if (nr.status == "ok" || nr.status == "partial" || nr.status == "soft_violation")
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

  GridMap grid(cfg.x_min, cfg.x_max, cfg.y_min, cfg.y_max, cfg.grid_mm, cfg.num_layers);

  // paint pads
  for (const auto &ob : pad_obstacles) {
    for (int ly = 0; ly < cfg.num_layers; ++ly)
      grid.paint_rect(ob.cx, ob.cy, ob.w + cfg.clearance_mm * 2, ob.h + cfg.clearance_mm * 2, ly,
                      ob.net_id);
  }

  // sort nets by priority desc
  std::vector<int> order(nets.size());
  for (size_t i = 0; i < nets.size(); ++i)
    order[i] = static_cast<int>(i);
  std::sort(order.begin(), order.end(), [&](int a, int b) {
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

    std::vector<int> layers = net.preferred_layers;
    if (layers.empty()) {
      for (int i = 0; i < cfg.num_layers; ++i)
        layers.push_back(i);
    }

    // Prim MST on anchors
    const size_t n = net.anchors.size();
    std::vector<char> in(n, 0);
    in[0] = 1;
    size_t in_count = 1;
    int open_edges = 0;
    std::string last_method;

    while (in_count < n) {
      double best_d = 1e100;
      size_t bi = 0, bj = 0;
      for (size_t i = 0; i < n; ++i)
        if (in[i])
          for (size_t j = 0; j < n; ++j)
            if (!in[j]) {
              double d = dist(net.anchors[i], net.anchors[j]);
              if (d < best_d) {
                best_d = d;
                bi = i;
                bj = j;
              }
            }
      in[bj] = 1;
      ++in_count;

      std::vector<Segment> segs;
      std::vector<Via> vias;
      std::string method;
      bool ok = route_edge(grid, cfg, net.anchors[bi], net.anchors[bj], net.net_id, layers,
                           net.width_mm, segs, vias, method);
      if (!ok) {
        if (cfg.soft_fallback) {
          Segment s{net.anchors[bi].x, net.anchors[bi].y, net.anchors[bj].x, net.anchors[bj].y,
                    layers.front(), net.net_id, net.width_mm};
          result.segments.push_back(s);
          result.total_length_mm += best_d;
          result.clearance_violations++;
          rep.segments++;
          rep.length_mm += best_d;
          last_method = "straight_fallback";
        } else {
          ++open_edges;
          last_method = "unrouted_edge";
        }
        continue;
      }
      last_method = method;
      for (auto &s : segs) {
        result.segments.push_back(s);
        double L = dist({s.x1, s.y1}, {s.x2, s.y2});
        result.total_length_mm += L;
        rep.length_mm += L;
        rep.segments++;
      }
      for (auto &v : vias) {
        result.vias.push_back(v);
        result.via_count++;
        rep.vias++;
      }
    }

    rep.method = last_method;
    if (open_edges && rep.segments == 0) {
      rep.status = "unrouted";
      result.unrouted.push_back(net.name);
    } else if (open_edges)
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

  // Optional GPU batch re-check of a sample of segments (stress / validation path)
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
