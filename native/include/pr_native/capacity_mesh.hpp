#pragma once
#include "types.hpp"
#include <string>
#include <utility>
#include <vector>

namespace pr {

/** Hierarchical capacity cell (tscircuit-inspired global planning). */
struct CapacityNode {
  int id = 0;
  double cx = 0, cy = 0;
  double width = 0, height = 0;
  int depth = 0;
  double capacity = 0;
  bool contains_target = false;
  bool contains_obstacle = false;
};

struct CapacityEdge {
  int a = 0;
  int b = 0;
};

struct CapacityMesh {
  std::vector<CapacityNode> nodes;
  std::vector<CapacityEdge> edges;
  int capacity_depth = 0;
  double effort = 0.55;
  double board_span_mm = 0;
  int path_queries = 0;
  int paths_found = 0;
};

struct CapacityPlanStats {
  int mesh_nodes = 0;
  int mesh_edges = 0;
  int sections_assigned = 0;
  int capacity_depth = 0;
  double effort = 0.55;
  double cell_mm = 0;
  int final_overflow = 0;
};

/**
 * Build a hierarchical capacity mesh over the board extent.
 * ``targets`` = pin/anchor positions that force refinement.
 * ``obstacles`` = pad centers for obstacle flags.
 */
CapacityMesh build_capacity_mesh(const RouteConfig &cfg,
                                 const std::vector<Vec2> &targets,
                                 const std::vector<Vec2> &obstacles,
                                 double effort = 0.55,
                                 int capacity_depth = -1 /* auto */);

/** A* over mesh nodes; returns node id path (empty if disconnected). */
std::vector<int> path_through_mesh(const CapacityMesh &mesh, Vec2 start, Vec2 goal);

/**
 * Native global capacity planning: fill ``topology_edge_layers`` and reorder
 * ``preferred_layers`` for every net that has topology_edges.
 * Returns summary stats for notes / Python.
 */
CapacityPlanStats plan_capacity_for_nets(std::vector<NetSpec> &nets,
                                         const RouteConfig &cfg,
                                         const std::vector<RectObs> &pads,
                                         double effort = 0.55);

double tuned_node_capacity(double width, double height, double via_diameter_mm,
                           double track_pitch_mm, int layer_count = 2);

int calculate_optimal_capacity_depth(double board_span_mm,
                                     double target_min_capacity = 0.55,
                                     int max_depth = 12,
                                     double via_diameter_mm = 0.6,
                                     double track_pitch_mm = 0.35);

} // namespace pr
