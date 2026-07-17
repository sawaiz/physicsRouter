#pragma once
#include "types.hpp"
#include <vector>

namespace pr {

/** Batch wirelength scoring (OpenMP over candidates). */
std::vector<ScoreResult> score_candidates_batch(
    const std::vector<ScoreInput> &candidates);

ScoreResult score_one(const ScoreInput &in);

} // namespace pr
