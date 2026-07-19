#include "pr_native/capacity_mesh.hpp"
#include <algorithm>
#include <cmath>
#include <queue>
#include <unordered_map>
#include <unordered_set>

namespace pr {
namespace {

inline double dist2(Vec2 a, Vec2 b) {
  const double dx = a.x - b.x, dy = a.y - b.y;
  return dx * dx + dy * dy;
}

inline bool contains_point(const CapacityNode &n, double x, double y) {
  return x >= n.cx - 0.5 * n.width && x <= n.cx + 0.5 * n.width &&
         y >= n.cy - 0.5 * n.height && y <= n.cy + 0.5 * n.height;
}

bool are_adjacent(const CapacityNode &a, const CapacityNode &b) {
  const double eps =
      0.08 * std::max({std::min(a.width, b.width), std::min(a.height, b.height), 0.2});
  const double dx = std::abs(a.cx - b.cx);
  const double dy = std::abs(a.cy - b.cy);
  const double half_w = 0.5 * (a.width + b.width);
  const double half_h = 0.5 * (a.height + b.height);
  if (dx < half_w * 0.35 && dy < half_h * 0.35)
    return false; // nested
  if (dx <= half_w + eps && dy <= 0.55 * std::max(a.height, b.height))
    return true;
  if (dy <= half_h + eps && dx <= 0.55 * std::max(a.width, b.width))
    return true;
  return false;
}

} // namespace

double tuned_node_capacity(double width, double height, double via_diameter_mm,
                           double track_pitch_mm, int layer_count) {
  if (width <= 0 || height <= 0)
    return 0.0;
  const double min_side = std::min(width, height);
  const double span = std::sqrt(width * height);
  const double pitch = std::max(0.08, via_diameter_mm * 0.5 + track_pitch_mm);
  const double via_ratio =
      min_side / std::max(0.08, via_diameter_mm + 0.5 * track_pitch_mm);
  const double via_ratio_factor =
      std::min(1.2, std::max(0.85, std::pow(via_ratio, 0.05)));
  const double via_length_across = (span * via_ratio_factor) / pitch;
  double cap = std::pow(via_length_across / 2.0, 1.1);
  if (layer_count <= 1 && cap > 1.0)
    return 1.0;
  return std::max(0.0, cap);
}

int calculate_optimal_capacity_depth(double board_span_mm,
                                     double target_min_capacity, int max_depth,
                                     double via_diameter_mm,
                                     double track_pitch_mm) {
  int depth = 0;
  double width = std::max(1.0, board_span_mm);
  while (depth < max_depth) {
    const double cap =
        tuned_node_capacity(width, width, via_diameter_mm, track_pitch_mm);
    if (cap <= target_min_capacity)
      break;
    width *= 0.5;
    ++depth;
  }
  return std::max(1, depth);
}

CapacityMesh build_capacity_mesh(const RouteConfig &cfg,
                                 const std::vector<Vec2> &targets,
                                 const std::vector<Vec2> &obstacles,
                                 double effort, int capacity_depth) {
  CapacityMesh mesh;
  effort = std::max(0.05, std::min(1.0, effort));
  mesh.effort = effort;

  const double x0 = cfg.x_min, x1 = cfg.x_max, y0 = cfg.y_min, y1 = cfg.y_max;
  const double span = std::max({x1 - x0, y1 - y0, 1.0});
  mesh.board_span_mm = span;
  const double cx = 0.5 * (x0 + x1);
  const double cy = 0.5 * (y0 + y1);
  const double side = span * 1.02;

  const double pitch = std::max(0.1, cfg.clearance_mm + 0.15);
  const double via_d = std::max(0.3, cfg.via_diameter_mm);
  int depth = capacity_depth;
  if (depth < 0) {
    depth = calculate_optimal_capacity_depth(
        side, 0.45 + 0.4 * (1.0 - effort), static_cast<int>(6 + 8 * effort), via_d,
        pitch);
  }
  mesh.capacity_depth = depth;

  auto flags = [&](double ncx, double ncy, double w, double h) {
    bool ct = false, co = false;
    const double hw = 0.5 * w, hh = 0.5 * h;
    for (const auto &t : targets) {
      if (std::abs(t.x - ncx) <= hw && std::abs(t.y - ncy) <= hh) {
        ct = true;
        break;
      }
    }
    for (const auto &o : obstacles) {
      if (std::abs(o.x - ncx) <= hw + 1.0 && std::abs(o.y - ncy) <= hh + 1.0) {
        co = true;
        break;
      }
    }
    return std::pair<bool, bool>{ct, co};
  };

  int next_id = 1;
  std::vector<CapacityNode> unfinished;
  unfinished.push_back({next_id++,
                        cx,
                        cy,
                        side,
                        side,
                        0,
                        tuned_node_capacity(side, side, via_d, pitch, cfg.num_layers),
                        true,
                        true});
  std::vector<CapacityNode> finished;
  int iters = 0;
  const int max_iters = 50000;
  while (!unfinished.empty() && iters++ < max_iters) {
    CapacityNode node = unfinished.back();
    unfinished.pop_back();
    auto fl = flags(node.cx, node.cy, node.width, node.height);
    node.contains_target = fl.first;
    node.contains_obstacle = fl.second;
    node.capacity =
        tuned_node_capacity(node.width, node.height, via_d, pitch, cfg.num_layers);

    const bool subdivide =
        node.depth < depth && (node.contains_target || node.contains_obstacle) &&
        node.capacity > 0.55 &&
        std::min(node.width, node.height) > std::max(0.4, 2.0 * pitch);
    if (!subdivide) {
      finished.push_back(node);
      continue;
    }
    const double hw = 0.5 * node.width, hh = 0.5 * node.height;
    for (double dx : {-0.25, 0.25})
      for (double dy : {-0.25, 0.25}) {
        CapacityNode child;
        child.id = next_id++;
        child.cx = node.cx + dx * node.width;
        child.cy = node.cy + dy * node.height;
        child.width = hw;
        child.height = hh;
        child.depth = node.depth + 1;
        unfinished.push_back(child);
      }
  }
  mesh.nodes = std::move(finished);

  // Spatial hash for adjacency
  double cell = 1.0;
  if (!mesh.nodes.empty()) {
    cell = 0.5 * mesh.nodes.front().width;
    for (const auto &n : mesh.nodes)
      cell = std::min(cell, 0.5 * n.width);
    cell = std::max(0.5, cell);
  }
  std::unordered_map<long long, std::vector<int>> buckets;
  auto key = [&](int ix, int iy) -> long long {
    return (static_cast<long long>(ix) << 32) ^ static_cast<unsigned>(iy);
  };
  for (size_t i = 0; i < mesh.nodes.size(); ++i) {
    const auto &n = mesh.nodes[i];
    const int ix = static_cast<int>(std::floor(n.cx / cell));
    const int iy = static_cast<int>(std::floor(n.cy / cell));
    buckets[key(ix, iy)].push_back(static_cast<int>(i));
  }

  for (size_t i = 0; i < mesh.nodes.size(); ++i) {
    const auto &a = mesh.nodes[i];
    const int ix = static_cast<int>(std::floor(a.cx / cell));
    const int iy = static_cast<int>(std::floor(a.cy / cell));
    for (int dx = -2; dx <= 2; ++dx)
      for (int dy = -2; dy <= 2; ++dy) {
        auto it = buckets.find(key(ix + dx, iy + dy));
        if (it == buckets.end())
          continue;
        for (int j : it->second) {
          if (static_cast<int>(i) >= j)
            continue;
          if (!are_adjacent(a, mesh.nodes[j]))
            continue;
          mesh.edges.push_back({a.id, mesh.nodes[j].id});
        }
      }
  }
  return mesh;
}

std::vector<int> path_through_mesh(const CapacityMesh &mesh, Vec2 start, Vec2 goal) {
  if (mesh.nodes.empty())
    return {};
  std::unordered_map<int, size_t> id_to_idx;
  for (size_t i = 0; i < mesh.nodes.size(); ++i)
    id_to_idx[mesh.nodes[i].id] = i;

  auto nearest = [&](Vec2 p) -> int {
    int best_id = -1;
    double best = 1e300;
    for (const auto &n : mesh.nodes) {
      if (contains_point(n, p.x, p.y))
        return n.id;
      const double d = dist2(p, {n.cx, n.cy});
      if (d < best) {
        best = d;
        best_id = n.id;
      }
    }
    return best_id;
  };
  const int start_id = nearest(start);
  const int goal_id = nearest(goal);
  if (start_id < 0 || goal_id < 0)
    return {};
  if (start_id == goal_id)
    return {start_id};

  std::unordered_map<int, std::vector<int>> adj;
  for (const auto &e : mesh.edges) {
    adj[e.a].push_back(e.b);
    adj[e.b].push_back(e.a);
  }

  using QItem = std::pair<double, int>;
  std::priority_queue<QItem, std::vector<QItem>, std::greater<QItem>> open;
  std::unordered_map<int, double> gscore;
  std::unordered_map<int, int> came;
  gscore[start_id] = 0.0;
  open.push({0.0, start_id});
  const auto &goal_node = mesh.nodes[id_to_idx[goal_id]];

  while (!open.empty()) {
    const int cur = open.top().second;
    open.pop();
    if (cur == goal_id) {
      std::vector<int> path;
      int c = cur;
      path.push_back(c);
      while (came.count(c)) {
        c = came[c];
        path.push_back(c);
      }
      std::reverse(path.begin(), path.end());
      return path;
    }
    const auto &cn = mesh.nodes[id_to_idx[cur]];
    for (int nxt : adj[cur]) {
      if (!id_to_idx.count(nxt))
        continue;
      const auto &nn = mesh.nodes[id_to_idx[nxt]];
      double step = std::hypot(nn.cx - cn.cx, nn.cy - cn.cy);
      if (nn.contains_obstacle && !nn.contains_target)
        step += 4.0;
      const double ng = gscore[cur] + step;
      if (!gscore.count(nxt) || ng + 1e-12 < gscore[nxt]) {
        gscore[nxt] = ng;
        came[nxt] = cur;
        const double h = std::hypot(nn.cx - goal_node.cx, nn.cy - goal_node.cy);
        open.push({ng + h, nxt});
      }
    }
  }
  return {};
}

CapacityPlanStats plan_capacity_for_nets(std::vector<NetSpec> &nets,
                                         const RouteConfig &cfg,
                                         const std::vector<RectObs> &pads,
                                         double effort) {
  CapacityPlanStats stats;
  stats.effort = effort;

  std::vector<Vec2> targets, obstacles;
  for (const auto &net : nets)
    for (const auto &a : net.anchors)
      targets.push_back(a);
  for (const auto &p : pads)
    obstacles.push_back({p.cx, p.cy});

  CapacityMesh mesh = build_capacity_mesh(cfg, targets, obstacles, effort, -1);
  stats.mesh_nodes = static_cast<int>(mesh.nodes.size());
  stats.mesh_edges = static_cast<int>(mesh.edges.size());
  stats.capacity_depth = mesh.capacity_depth;

  // Mean leaf width → cell for overflow accounting
  double cell = 1.0;
  if (!mesh.nodes.empty()) {
    double sum = 0;
    for (const auto &n : mesh.nodes)
      sum += n.width;
    cell = std::max(0.45, 0.55 * sum / mesh.nodes.size());
  }
  stats.cell_mm = cell;

  const double pitch = std::max(0.1, cfg.clearance_mm + 0.15);
  const int cell_capacity =
      std::max(1, static_cast<int>(std::floor(cell / std::max(0.1, pitch))));

  // Resource key: (ix, iy, layer) hashed
  auto rkey = [](int ix, int iy, int layer) -> long long {
    return (static_cast<long long>(ix & 0xfffff) << 24) ^
           (static_cast<long long>(iy & 0xfffff) << 4) ^
           static_cast<long long>(layer & 0xf);
  };

  // Collect sections (net, edge_index)
  struct Section {
    int net_i;
    int edge_i;
    int u, v;
    double length;
    double priority;
  };
  std::vector<Section> sections;
  for (size_t ni = 0; ni < nets.size(); ++ni) {
    auto &net = nets[ni];
    if (net.topology_edges.empty()) {
      // Build Euclidean MST-like chain of nearest pairs for multipin
      if (net.anchors.size() < 2)
        continue;
      // Prim edges as topology if none supplied
      std::vector<char> in(net.anchors.size(), 0);
      in[0] = 1;
      size_t count = 1;
      while (count < net.anchors.size()) {
        double best = 1e300;
        int bi = -1, bj = -1;
        for (size_t i = 0; i < net.anchors.size(); ++i)
          if (in[i])
            for (size_t j = 0; j < net.anchors.size(); ++j)
              if (!in[j]) {
                const double d = std::hypot(net.anchors[i].x - net.anchors[j].x,
                                            net.anchors[i].y - net.anchors[j].y);
                if (d < best) {
                  best = d;
                  bi = static_cast<int>(i);
                  bj = static_cast<int>(j);
                }
              }
        if (bi < 0)
          break;
        net.topology_edges.push_back({bi, bj});
        in[bj] = 1;
        ++count;
      }
    }
    for (size_t ei = 0; ei < net.topology_edges.size(); ++ei) {
      const auto &e = net.topology_edges[ei];
      if (e.first < 0 || e.second < 0 ||
          e.first >= static_cast<int>(net.anchors.size()) ||
          e.second >= static_cast<int>(net.anchors.size()))
        continue;
      const auto &a = net.anchors[e.first];
      const auto &b = net.anchors[e.second];
      sections.push_back({static_cast<int>(ni), static_cast<int>(ei), e.first,
                          e.second,
                          std::hypot(a.x - b.x, a.y - b.y), net.priority});
    }
  }
  std::stable_sort(sections.begin(), sections.end(),
                   [](const Section &a, const Section &b) {
                     if (a.priority != b.priority)
                       return a.priority > b.priority;
                     return a.length > b.length;
                   });

  std::unordered_map<long long, double> history;
  int overflow_final = 0;
  const int max_iters = 10;
  std::vector<int> best_layer(sections.size(), 0);

  for (int iter = 0; iter < max_iters; ++iter) {
    std::unordered_map<long long, int> occupancy;
    overflow_final = 0;
    for (size_t si = 0; si < sections.size(); ++si) {
      const auto &sec = sections[si];
      auto &net = nets[sec.net_i];
      const Vec2 a = net.anchors[sec.u];
      const Vec2 b = net.anchors[sec.v];

      auto mesh_path = path_through_mesh(mesh, a, b);
      mesh.path_queries++;
      if (!mesh_path.empty())
        mesh.paths_found++;

      int best_ly = 0;
      double best_cost = 1e300;
      std::vector<int> layer_candidates;
      if (!net.preferred_layers.empty())
        layer_candidates = net.preferred_layers;
      else
        for (int ly = 0; ly < cfg.num_layers; ++ly)
          layer_candidates.push_back(ly);

      for (int ly : layer_candidates) {
        // Sample cells along segment
        int samples = std::max(1, static_cast<int>(std::ceil(sec.length / (0.45 * cell))));
        double cost = sec.length;
        int overflow = 0, present = 0;
        double hist = 0;
        for (int s = 0; s <= samples; ++s) {
          const double t = samples ? static_cast<double>(s) / samples : 0.0;
          const double x = a.x + (b.x - a.x) * t;
          const double y = a.y + (b.y - a.y) * t;
          const int ix = static_cast<int>(std::floor(x / cell));
          const int iy = static_cast<int>(std::floor(y / cell));
          const long long k = rkey(ix, iy, ly);
          const int dem = occupancy[k];
          present += dem;
          overflow += std::max(0, dem + 1 - cell_capacity);
          hist += history[k];
        }
        // Prefer mesh corridor length
        if (!mesh_path.empty())
          cost += 0.15 * std::max(0, static_cast<int>(mesh_path.size()) - 1);
        else
          cost += 8.0;
        cost += 2.5 * present + 18.0 * overflow + hist;
        // Slight outer-layer preference for signal
        cost += 0.05 * ly;
        if (cost < best_cost) {
          best_cost = cost;
          best_ly = ly;
        }
      }
      best_layer[si] = best_ly;
      // Commit occupancy on best layer
      int samples = std::max(1, static_cast<int>(std::ceil(sec.length / (0.45 * cell))));
      for (int s = 0; s <= samples; ++s) {
        const double t = samples ? static_cast<double>(s) / samples : 0.0;
        const double x = a.x + (b.x - a.x) * t;
        const double y = a.y + (b.y - a.y) * t;
        const int ix = static_cast<int>(std::floor(x / cell));
        const int iy = static_cast<int>(std::floor(y / cell));
        occupancy[rkey(ix, iy, best_ly)] += 1;
      }
    }
    // Overflow + history
    int overflow = 0;
    for (const auto &kv : occupancy) {
      const int dem = kv.second;
      if (dem > cell_capacity) {
        overflow += dem - cell_capacity;
        history[kv.first] += 4.0 * (dem - cell_capacity);
      }
    }
    overflow_final = overflow;
    if (overflow == 0)
      break;
  }
  stats.final_overflow = overflow_final;

  // Write topology_edge_layers and reorder preferred_layers
  for (size_t si = 0; si < sections.size(); ++si) {
    const auto &sec = sections[si];
    auto &net = nets[sec.net_i];
    if (net.topology_edge_layers.size() < net.topology_edges.size())
      net.topology_edge_layers.resize(net.topology_edges.size(), 0);
    if (sec.edge_i >= 0 &&
        static_cast<size_t>(sec.edge_i) < net.topology_edge_layers.size())
      net.topology_edge_layers[sec.edge_i] = best_layer[si];
  }
  for (auto &net : nets) {
    if (net.topology_edge_layers.empty())
      continue;
    // Count layers
    std::vector<int> counts(std::max(1, cfg.num_layers), 0);
    for (int ly : net.topology_edge_layers)
      if (ly >= 0 && ly < cfg.num_layers)
        counts[ly]++;
    std::vector<int> order(cfg.num_layers);
    for (int i = 0; i < cfg.num_layers; ++i)
      order[i] = i;
    std::stable_sort(order.begin(), order.end(), [&](int a, int b) {
      if (counts[a] != counts[b])
        return counts[a] > counts[b];
      return a < b;
    });
    net.preferred_layers = order;
  }
  stats.sections_assigned = static_cast<int>(sections.size());
  return stats;
}

} // namespace pr
