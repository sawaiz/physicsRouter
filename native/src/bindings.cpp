#include "pr_native/exact.hpp"
#include "pr_native/gpu.hpp"
#include "pr_native/router.hpp"
#include "pr_native/score.hpp"
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

PYBIND11_MODULE(pr_native, m) {
  m.doc() = "physicsRouter native C++ core — isotropic free-angle (OpenMP + OpenCL)";

  m.def("version", []() { return pr::native_version(); });

  py::class_<pr::Vec2>(m, "Vec2")
      .def(py::init<>())
      .def(py::init<double, double>())
      .def_readwrite("x", &pr::Vec2::x)
      .def_readwrite("y", &pr::Vec2::y);

  py::class_<pr::RectObs>(m, "RectObs")
      .def(py::init<>())
      .def_readwrite("cx", &pr::RectObs::cx)
      .def_readwrite("cy", &pr::RectObs::cy)
      .def_readwrite("w", &pr::RectObs::w)
      .def_readwrite("h", &pr::RectObs::h)
      .def_readwrite("rotation_deg", &pr::RectObs::rotation_deg)
      .def_readwrite("net_id", &pr::RectObs::net_id)
      .def_readwrite("layers", &pr::RectObs::layers)
      .def_readwrite("is_pad", &pr::RectObs::is_pad);

  py::class_<pr::NetSpec>(m, "NetSpec")
      .def(py::init<>())
      .def_readwrite("net_id", &pr::NetSpec::net_id)
      .def_readwrite("name", &pr::NetSpec::name)
      .def_readwrite("anchors", &pr::NetSpec::anchors)
      .def_readwrite("anchor_layers", &pr::NetSpec::anchor_layers)
      .def_readwrite("topology_edges", &pr::NetSpec::topology_edges)
      .def_readwrite("topology_edge_layers", &pr::NetSpec::topology_edge_layers)
      .def_readwrite("anchor_via_sites", &pr::NetSpec::anchor_via_sites)
      .def_readwrite("priority", &pr::NetSpec::priority)
      .def_readwrite("width_mm", &pr::NetSpec::width_mm)
      .def_readwrite("preferred_layers", &pr::NetSpec::preferred_layers)
      .def_readwrite("use_copper_area", &pr::NetSpec::use_copper_area)
      .def_readwrite("area_layer", &pr::NetSpec::area_layer)
      .def_readwrite("area_margin_mm", &pr::NetSpec::area_margin_mm)
      .def_readwrite("area_priority", &pr::NetSpec::area_priority);

  py::class_<pr::CongestionCell>(m, "CongestionCell")
      .def(py::init<>())
      .def_readwrite("ix", &pr::CongestionCell::ix)
      .def_readwrite("iy", &pr::CongestionCell::iy)
      .def_readwrite("layer", &pr::CongestionCell::layer)
      .def_readwrite("cost", &pr::CongestionCell::cost);

  py::class_<pr::RouteConfig>(m, "RouteConfig")
      .def(py::init<>())
      .def_readwrite("x_min", &pr::RouteConfig::x_min)
      .def_readwrite("x_max", &pr::RouteConfig::x_max)
      .def_readwrite("y_min", &pr::RouteConfig::y_min)
      .def_readwrite("y_max", &pr::RouteConfig::y_max)
      .def_readwrite("board_outline", &pr::RouteConfig::board_outline)
      .def_readwrite("grid_mm", &pr::RouteConfig::grid_mm)
      .def_readwrite("clearance_mm", &pr::RouteConfig::clearance_mm)
      .def_readwrite("edge_clearance_mm", &pr::RouteConfig::edge_clearance_mm)
      .def_readwrite("via_diameter_mm", &pr::RouteConfig::via_diameter_mm)
      .def_readwrite("via_drill_mm", &pr::RouteConfig::via_drill_mm)
      .def_readwrite("min_hole_to_hole_mm",
                     &pr::RouteConfig::min_hole_to_hole_mm)
      .def_readwrite("num_layers", &pr::RouteConfig::num_layers)
      .def_readwrite("max_expansions", &pr::RouteConfig::max_expansions)
      .def_readwrite("soft_fallback", &pr::RouteConfig::soft_fallback)
      .def_readwrite("allow_vias", &pr::RouteConfig::allow_vias)
      .def_readwrite("allow_blind_buried_vias",
                     &pr::RouteConfig::allow_blind_buried_vias)
      .def_readwrite("use_gpu", &pr::RouteConfig::use_gpu)
      .def_readwrite("isotropic", &pr::RouteConfig::isotropic)
      .def_readwrite("post_rubberband", &pr::RouteConfig::post_rubberband)
      .def_readwrite("via_minimize", &pr::RouteConfig::via_minimize)
      .def_readwrite("atomic_nets", &pr::RouteConfig::atomic_nets)
      .def_readwrite("congestion_cell_mm", &pr::RouteConfig::congestion_cell_mm)
      .def_readwrite("congestion", &pr::RouteConfig::congestion)
      .def_readwrite("threads", &pr::RouteConfig::threads);

  py::class_<pr::Segment>(m, "Segment")
      .def_readonly("x1", &pr::Segment::x1)
      .def_readonly("y1", &pr::Segment::y1)
      .def_readonly("x2", &pr::Segment::x2)
      .def_readonly("y2", &pr::Segment::y2)
      .def_readonly("layer", &pr::Segment::layer)
      .def_readonly("net_id", &pr::Segment::net_id)
      .def_readonly("width_mm", &pr::Segment::width_mm);

  py::class_<pr::Via>(m, "Via")
      .def_readonly("x", &pr::Via::x)
      .def_readonly("y", &pr::Via::y)
      .def_readonly("net_id", &pr::Via::net_id)
      .def_readonly("size_mm", &pr::Via::size_mm)
      .def_readonly("drill_mm", &pr::Via::drill_mm)
      .def_readonly("layer_a", &pr::Via::layer_a)
      .def_readonly("layer_b", &pr::Via::layer_b)
      .def_readonly("reason", &pr::Via::reason)
      .def_readonly("alternatives_considered", &pr::Via::alternatives_considered);

  py::class_<pr::CopperArea>(m, "CopperArea")
      .def_readonly("outline", &pr::CopperArea::outline)
      .def_readonly("layer", &pr::CopperArea::layer)
      .def_readonly("net_id", &pr::CopperArea::net_id)
      .def_readonly("clearance_mm", &pr::CopperArea::clearance_mm)
      .def_readonly("min_thickness_mm", &pr::CopperArea::min_thickness_mm)
      .def_readonly("priority", &pr::CopperArea::priority);

  py::class_<pr::NetReport>(m, "NetReport")
      .def_readonly("net_id", &pr::NetReport::net_id)
      .def_readonly("name", &pr::NetReport::name)
      .def_readonly("pins", &pr::NetReport::pins)
      .def_readonly("length_mm", &pr::NetReport::length_mm)
      .def_readonly("segments", &pr::NetReport::segments)
      .def_readonly("vias", &pr::NetReport::vias)
      .def_readonly("status", &pr::NetReport::status)
      .def_readonly("method", &pr::NetReport::method);

  py::class_<pr::RouteResult>(m, "RouteResult")
      .def_readonly("segments", &pr::RouteResult::segments)
      .def_readonly("vias", &pr::RouteResult::vias)
      .def_readonly("areas", &pr::RouteResult::areas)
      .def_readonly("total_length_mm", &pr::RouteResult::total_length_mm)
      .def_readonly("via_count", &pr::RouteResult::via_count)
      .def_readonly("clearance_violations", &pr::RouteResult::clearance_violations)
      .def_readonly("unrouted", &pr::RouteResult::unrouted)
      .def_readonly("net_reports", &pr::RouteResult::net_reports)
      .def_readonly("notes", &pr::RouteResult::notes)
      .def_readonly("quality_score", &pr::RouteResult::quality_score)
      .def_readonly("quality_grade", &pr::RouteResult::quality_grade)
      .def_readonly("elapsed_ms", &pr::RouteResult::elapsed_ms)
      .def_readonly("used_native", &pr::RouteResult::used_native)
      .def_readonly("used_gpu", &pr::RouteResult::used_gpu);

  m.def(
      "route_board",
      [](const std::vector<pr::NetSpec> &nets, const pr::RouteConfig &cfg,
         const std::vector<pr::RectObs> &obs) {
        py::gil_scoped_release release;
        return pr::route_board(nets, cfg, obs, nullptr);
      },
      py::arg("nets"), py::arg("cfg"), py::arg("obstacles") = std::vector<pr::RectObs>{});

  // Exact-geometry clearance authority + free-angle search (the only router core)
  py::class_<pr::ExactMap>(m, "ExactMap")
      .def(py::init<double, double, double, double, double, int>(), py::arg("x_min"),
           py::arg("x_max"), py::arg("y_min"), py::arg("y_max"), py::arg("clearance_mm"),
           py::arg("num_layers"))
      .def("add_rect", &pr::ExactMap::add_rect, py::arg("cx"), py::arg("cy"), py::arg("w"),
           py::arg("h"), py::arg("layer"), py::arg("net"))
      .def("add_painted", &pr::ExactMap::add_painted, py::arg("x1"), py::arg("y1"),
           py::arg("x2"), py::arg("y2"), py::arg("layer"), py::arg("width"), py::arg("net"))
      .def(
          "set_outline",
          [](pr::ExactMap &em, const std::vector<std::pair<double, double>> &pts) {
            std::vector<pr::Vec2> poly;
            poly.reserve(pts.size());
            for (const auto &p : pts)
              poly.push_back({p.first, p.second});
            em.set_outline(poly);
          },
          py::arg("pts"))
      .def("has_outline", &pr::ExactMap::has_outline)
      .def("point_in_outline", &pr::ExactMap::point_in_outline, py::arg("x"), py::arg("y"))
      .def("in_bounds", &pr::ExactMap::in_bounds)
      .def("blocked", &pr::ExactMap::blocked, py::arg("x"), py::arg("y"), py::arg("layer"),
           py::arg("net"))
      .def("segment_blocked", &pr::ExactMap::segment_blocked, py::arg("x1"), py::arg("y1"),
           py::arg("x2"), py::arg("y2"), py::arg("layer"), py::arg("net"),
           py::arg("width_mm") = 0.25);

  m.def(
      "free_angle_route_exact",
      [](const pr::ExactMap &em, double sx, double sy, double gx, double gy, int layer, int net,
         double grid_mm, int max_expansions, double width_mm, double cong_cell_mm,
         const std::vector<int64_t> &cong_keys, const std::vector<double> &cong_costs)
          -> py::object {
        pr::CongestionView cong;
        const pr::CongestionView *cptr = nullptr;
        if (!cong_keys.empty() && cong_keys.size() == cong_costs.size()) {
          cong.cell_mm = cong_cell_mm > 0 ? cong_cell_mm : 1.0;
          for (size_t i = 0; i < cong_keys.size(); ++i)
            cong.cells[cong_keys[i]] = cong_costs[i];
          cptr = &cong;
        }
        std::string method;
        std::vector<pr::Vec2> path;
        {
          // Board-wide negotiated routing evaluates independent conflict nets
          // concurrently. Release the GIL for the C++ geometry search while
          // retaining Python ownership only for argument/result conversion.
          py::gil_scoped_release release;
          path = pr::free_angle_route_exact(em, {sx, sy}, {gx, gy}, layer, net,
                                            grid_mm, max_expansions, width_mm,
                                            cptr, &method);
        }
        if (path.empty())
          return py::none();
        py::list pts;
        for (const auto &p : path)
          pts.append(py::make_tuple(p.x, p.y));
        return py::make_tuple(pts, method);
      },
      py::arg("map"), py::arg("sx"), py::arg("sy"), py::arg("gx"), py::arg("gy"),
      py::arg("layer"), py::arg("net"), py::arg("grid_mm") = 0.1,
      py::arg("max_expansions") = 8000, py::arg("width_mm") = 0.25,
      py::arg("cong_cell_mm") = 1.0, py::arg("cong_keys") = std::vector<int64_t>{},
      py::arg("cong_costs") = std::vector<double>{});

  m.def(
      "rubberband_exact",
      [](const pr::ExactMap &em, const std::vector<std::pair<double, double>> &path, int layer,
         int net, double width_mm) {
        std::vector<pr::Vec2> in;
        in.reserve(path.size());
        for (const auto &p : path)
          in.push_back({p.first, p.second});
        auto out = pr::rubberband_exact(em, in, layer, net, width_mm);
        py::list pts;
        for (const auto &p : out)
          pts.append(py::make_tuple(p.x, p.y));
        return pts;
      },
      py::arg("map"), py::arg("path"), py::arg("layer"), py::arg("net"),
      py::arg("width_mm") = 0.25);

  m.def(
      "drc_check",
      [](const std::vector<std::tuple<double, double, double, double, double, int, int>> &segs,
         const std::vector<std::tuple<double, double, double, int>> &vias, double clearance_mm,
         int max_violations) {
        std::vector<pr::DrcSeg> ds;
        ds.reserve(segs.size());
        for (const auto &t : segs) {
          pr::DrcSeg s;
          std::tie(s.x1, s.y1, s.x2, s.y2, s.width, s.layer, s.net) = t;
          ds.push_back(s);
        }
        std::vector<pr::DrcVia> dv;
        dv.reserve(vias.size());
        for (const auto &t : vias) {
          pr::DrcVia v;
          std::tie(v.x, v.y, v.size, v.net) = t;
          dv.push_back(v);
        }
        auto res = pr::drc_check(ds, dv, clearance_mm, max_violations);
        py::list out;
        for (const auto &v : res) {
          py::dict d;
          d["kind"] = v.kind == 1 ? "short" : "spacing";
          d["net_a"] = v.net_a;
          d["net_b"] = v.net_b;
          d["layer"] = v.layer;
          d["x"] = v.x;
          d["y"] = v.y;
          d["dist"] = v.dist;
          d["need"] = v.need;
          out.append(d);
        }
        return out;
      },
      py::arg("segments"), py::arg("vias") = std::vector<std::tuple<double, double, double, int>>{},
      py::arg("clearance_mm") = 0.2, py::arg("max_violations") = 200);

  m.def("gpu_probe", []() {
    auto s = pr::gpu_probe();
    py::dict d;
    d["available"] = s.available;
    d["device_name"] = s.device_name;
    d["backend"] = s.backend;
    return d;
  });

  m.def(
      "score_batch",
      [](const std::vector<std::vector<std::pair<double, double>>> &centers_list,
         const std::vector<std::vector<std::vector<int>>> &net_pins_list,
         const std::vector<std::vector<double>> &weights_list) {
        std::vector<pr::ScoreInput> inputs;
        for (size_t i = 0; i < centers_list.size(); ++i) {
          pr::ScoreInput in;
          for (auto &p : centers_list[i])
            in.centers.push_back({p.first, p.second});
          if (i < net_pins_list.size())
            in.net_pins = net_pins_list[i];
          if (i < weights_list.size())
            in.net_weights = weights_list[i];
          inputs.push_back(std::move(in));
        }
        auto res = pr::score_candidates_batch(inputs);
        py::list out;
        for (auto &r : res) {
          py::dict d;
          d["weighted_wirelength"] = r.weighted_wirelength;
          d["total"] = r.total;
          d["elapsed_ms"] = r.elapsed_ms;
          out.append(d);
        }
        return out;
      });
}
