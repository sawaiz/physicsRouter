#pragma once
#include "grid.hpp"
#include "types.hpp"
#include <functional>

namespace pr {

using ProgressFn = std::function<void(int done, int total, const std::string &net,
                                      const std::string &stage)>;

/** High-performance free-angle / grid A* multi-net router (isotropic). */
RouteResult route_board(const std::vector<NetSpec> &nets, const RouteConfig &cfg,
                        const std::vector<RectObs> &pad_obstacles,
                        ProgressFn progress = nullptr);

/** Single point-to-point on one layer; returns polyline or empty.
 *  When isotropic=true, tries perpendicular bulges before L-bends/A*.
 */
std::vector<Vec2> route_point(const GridMap &grid, Vec2 start, Vec2 goal, int layer,
                              int net_id, int max_expansions, bool isotropic = true);

/** Dayan-style rubberband on a polyline under grid clearance. */
std::vector<Vec2> rubberband_path(const std::vector<Vec2> &path, const GridMap &g, int layer,
                                  int net_id);

/** Drop vias when both stubs can legally merge onto one layer. */
int remove_redundant_vias(RouteResult &r, GridMap &grid, const RouteConfig &cfg);

void compute_quality(RouteResult &r);

/** Library version string. */
const char *native_version();

} // namespace pr
