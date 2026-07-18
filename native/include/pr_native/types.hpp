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
  int net_id = -1; // -1 = blocks all
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
  int layer_a = 0, layer_b = 1;
  std::string reason; // explainable: why this via was inserted
  int alternatives_considered = 0;
};

struct NetSpec {
  int net_id = 0;
  std::string name;
  std::vector<Vec2> anchors; // unique pin positions
  double priority = 1.0;
  double width_mm = 0.25;
  std::vector<int> preferred_layers; // empty = all
};

struct RouteConfig {
  double x_min = 0, x_max = 100, y_min = 0, y_max = 100;
  double grid_mm = 0.1; // fine free-angle default (matches Python pipeline)
  double clearance_mm = 0.2;
  int num_layers = 2;
  int max_expansions = 4000;
  bool soft_fallback = false;
  bool allow_vias = true;
  bool use_gpu = true;
  bool isotropic = true;     // any-angle detours (TopoR-style)
  bool post_rubberband = true;
  bool via_minimize = false; // connectivity/clearance beat via count
  int threads = 0; // 0 = auto
  // HALO-style polar ring: prefer radial+arc paths about (ring_cx, ring_cy)
  bool ring_mode = false;
  double ring_cx = 0.0;
  double ring_cy = 0.0;
  double ring_track_r = 0.0; // 0 = auto mid-radius of start/goal
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
