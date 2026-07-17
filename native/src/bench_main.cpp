#include "pr_native/gpu.hpp"
#include "pr_native/router.hpp"
#include "pr_native/score.hpp"
#include <cstdio>
#include <random>
#include <vector>

int main() {
  auto gpu = pr::gpu_probe();
  std::printf("GPU: available=%d backend=%s device=%s\n", (int)gpu.available,
              gpu.backend.c_str(), gpu.device_name.c_str());

  pr::RouteConfig cfg;
  cfg.x_min = -15;
  cfg.x_max = 15;
  cfg.y_min = -15;
  cfg.y_max = 15;
  cfg.grid_mm = 0.5;
  cfg.clearance_mm = 0.2;
  cfg.num_layers = 2;
  cfg.max_expansions = 3000;
  cfg.soft_fallback = false;
  cfg.use_gpu = true;

  std::mt19937 rng(42);
  std::uniform_real_distribution<double> dist(-12, 12);

  std::vector<pr::NetSpec> nets;
  for (int i = 0; i < 40; ++i) {
    pr::NetSpec n;
    n.net_id = i;
    n.name = "N" + std::to_string(i);
    n.priority = 1.0 + (i % 5);
    n.width_mm = 0.25;
    n.anchors = {{dist(rng), dist(rng)}, {dist(rng), dist(rng)}, {dist(rng), dist(rng)}};
    nets.push_back(n);
  }

  std::vector<pr::RectObs> obs;
  for (int i = 0; i < 20; ++i)
    obs.push_back({dist(rng), dist(rng), 0.8, 0.8, -1});

  auto r = pr::route_board(nets, cfg, obs);
  std::printf("Route: segs=%zu vias=%d length=%.2f grade=%s ms=%.2f gpu=%d\n",
              r.segments.size(), r.via_count, r.total_length_mm, r.quality_grade.c_str(),
              r.elapsed_ms, (int)r.used_gpu);
  for (auto &n : r.notes)
    std::printf("  note: %s\n", n.c_str());

  // score batch
  std::vector<pr::ScoreInput> cands(64);
  for (auto &c : cands) {
    c.centers.resize(30);
    for (auto &p : c.centers)
      p = {dist(rng), dist(rng)};
    c.net_pins = {{0, 1, 2}, {3, 4}, {5, 6, 7, 8}};
    c.net_weights = {2.0, 1.0, 1.5};
  }
  auto scores = pr::score_candidates_batch(cands);
  double sum = 0;
  for (auto &s : scores)
    sum += s.total;
  std::printf("Score batch: n=%zu sum_wl=%.2f first_ms=%.4f\n", scores.size(), sum,
              scores.empty() ? 0.0 : scores[0].elapsed_ms);
  return 0;
}
