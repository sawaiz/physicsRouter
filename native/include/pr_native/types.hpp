#pragma once
#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace pr {

struct Vec2 {
  double x = 0, y = 0;
};

struct RectObs {
  double cx = 0, cy = 0, w = 0, h = 0;
  // Board-space orientation. Keeping the real pad angle avoids turning
  // fine-pitch rotated pads into an artificial axis-aligned keepout wall.
  double rotation_deg = 0;
  int net_id = -1; // -1 = blocks all
  std::vector<int> layers; // empty = all copper layers
  // Identifies physical pad copper for the exact via-site distance check.
  // Tracks may terminate on their own pad, but vias may never overlap a pad.
  bool is_pad = false;
};

struct Segment {
  double x1 = 0, y1 = 0, x2 = 0, y2 = 0;
  int layer = 0;
  int net_id = 0;
  double width_mm = 0.25;
};

struct Via {
  double x = 0, y = 0;
  int net_id = 0;
  double size_mm = 0.8;
  double drill_mm = 0.4;
  int layer_a = 0, layer_b = 1;
  std::string reason; // explainable: why this via was inserted
  int alternatives_considered = 0;
};

/** Refillable copper zone boundary.
 *
 * The polygon is native router output, while the PCB editor remains the fill
 * authority so clearances, thermals, and same-layer cut-outs use fab rules.
 */
struct CopperArea {
  std::vector<Vec2> outline;
  int layer = 0;
  int net_id = 0;
  double clearance_mm = 0.2;
  double min_thickness_mm = 0.25;
  int priority = 0;
};

struct NetSpec {
  int net_id = 0;
  std::string name;
  std::vector<Vec2> anchors; // unique pin positions
  // Copper layers physically reachable at each anchor (SMD pad vs plated hole).
  // Empty outer vector or empty entry means all preferred layers.
  std::vector<std::vector<int>> anchor_layers;
  // Advisory graph-theory spanning tree. Each pair indexes anchors; the
  // native router prefers these frontier edges and falls back to any legal
  // edge when geometrization invalidates the abstract topology.
  std::vector<std::pair<int, int>> topology_edges;
  // Per-topology-edge global layer assignment. Entries align with
  // topology_edges and are advisory when an exact detailed route is blocked.
  std::vector<int> topology_edge_layers;
  // Exact preflighted offset-via sites for every anchor. These are tried
  // before generic maze-derived sites so detailed routing consumes the same
  // finite access resources as global planning.
  std::vector<std::vector<Vec2>> anchor_via_sites;
  double priority = 1.0;
  double width_mm = 0.25;
  std::vector<int> preferred_layers; // empty = all
  bool use_copper_area = false;
  int area_layer = -1; // -1 = first preferred layer
  double area_margin_mm = 0.8;
  int area_priority = 0;
};

/** Sparse historical routing-resource cost supplied by the PathFinder host. */
struct CongestionCell {
  int ix = 0;
  int iy = 0;
  int layer = 0;
  double cost = 0.0;
};

struct RouteConfig {
  double x_min = 0, x_max = 100, y_min = 0, y_max = 100;
  std::vector<Vec2> board_outline; // optional true Edge.Cuts polygon
  double grid_mm = 0.1; // fine free-angle default (matches Python pipeline)
  double clearance_mm = 0.2;
  double edge_clearance_mm = 0.01;
  double via_diameter_mm = 0.8;
  double via_drill_mm = 0.4;
  double min_hole_to_hole_mm = 0.25;
  int num_layers = 2;
  int max_expansions = 4000;
  bool soft_fallback = false;
  bool allow_vias = true;
  bool allow_blind_buried_vias = false;
  bool use_gpu = true;
  bool isotropic = true;     // any-angle detours (TopoR-style)
  bool post_rubberband = true;
  bool via_minimize = false; // connectivity/clearance beat via count
  bool atomic_nets = true;   // commit a net only when every anchor connects
  double congestion_cell_mm = 0.5;
  std::vector<CongestionCell> congestion;
  int threads = 0; // 0 = auto
  // Capacity-mesh global planning (tscircuit-inspired) before detailed route
  bool enable_capacity_mesh = true;
  double capacity_effort = 0.55; // 0..1 depth / refinement
  int capacity_depth = -1;       // -1 = auto from board span
};

struct NetReport {
  int net_id = 0;
  std::string name;
  int pins = 0;
  double length_mm = 0;
  int segments = 0;
  int vias = 0;
  std::string status; // ok | partial | unrouted | soft_violation
  std::string method;
};

struct RouteResult {
  std::vector<Segment> segments;
  std::vector<Via> vias;
  std::vector<CopperArea> areas;
  double total_length_mm = 0;
  int via_count = 0;
  int clearance_violations = 0;
  std::vector<std::string> unrouted;
  std::vector<NetReport> net_reports;
  std::vector<std::string> notes;
  double quality_score = 0;
  std::string quality_grade = "F";
  double elapsed_ms = 0;
  bool used_native = true;
  bool used_gpu = false;
};

struct ScoreInput {
  // Component centers
  std::vector<Vec2> centers;
  std::vector<int> locked; // 0/1
  // nets: list of pin component indices
  std::vector<std::vector<int>> net_pins;
  std::vector<double> net_weights;
};

struct ScoreResult {
  double weighted_wirelength = 0;
  double total = 0;
  double elapsed_ms = 0;
};

} // namespace pr
