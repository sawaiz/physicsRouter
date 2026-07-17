#pragma once
#include "grid.hpp"
#include "types.hpp"
#include <functional>

namespace pr {

using ProgressFn = std::function<void(int done, int total, const std::string &net,
                                      const std::string &stage)>;

/** High-performance free-angle / grid A* multi-net router. */
RouteResult route_board(const std::vector<NetSpec> &nets, const RouteConfig &cfg,
                        const std::vector<RectObs> &pad_obstacles,
                        ProgressFn progress = nullptr);

/** Single point-to-point on one layer; returns polyline or empty. */
std::vector<Vec2> route_point(const GridMap &grid, Vec2 start, Vec2 goal, int layer,
                              int net_id, int max_expansions);

void compute_quality(RouteResult &r);

} // namespace pr
