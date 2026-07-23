#include "pr_native/score.hpp"
#include <chrono>
#include <cmath>
#ifdef PR_HAS_OPENMP
#include <omp.h>
#endif

namespace pr {

ScoreResult score_one(const ScoreInput &in) {
  ScoreResult r;
  auto t0 = std::chrono::steady_clock::now();
  double wl = 0;
  for (size_t n = 0; n < in.net_pins.size(); ++n) {
    const auto &pins = in.net_pins[n];
    if (pins.size() < 2)
      continue;
    double w = n < in.net_weights.size() ? in.net_weights[n] : 1.0;
    // HPWL on pin bbox
    double minx = 1e100, maxx = -1e100, miny = 1e100, maxy = -1e100;
    for (int pi : pins) {
      if (pi < 0 || static_cast<size_t>(pi) >= in.centers.size())
        continue;
      minx = std::min(minx, in.centers[pi].x);
      maxx = std::max(maxx, in.centers[pi].x);
      miny = std::min(miny, in.centers[pi].y);
      maxy = std::max(maxy, in.centers[pi].y);
    }
    wl += w * ((maxx - minx) + (maxy - miny));
  }
  r.weighted_wirelength = wl;
  r.total = wl;
  r.elapsed_ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
                     .count();
  return r;
}

std::vector<ScoreResult> score_candidates_batch(const std::vector<ScoreInput> &candidates) {
  std::vector<ScoreResult> out(candidates.size());
#ifdef PR_HAS_OPENMP
#pragma omp parallel for schedule(static)
#endif
  for (int i = 0; i < static_cast<int>(candidates.size()); ++i) {
    out[static_cast<size_t>(i)] = score_one(candidates[static_cast<size_t>(i)]);
  }
  return out;
}

} // namespace pr
