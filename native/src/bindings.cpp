#include "pr_native/gpu.hpp"
#include "pr_native/router.hpp"
#include "pr_native/score.hpp"
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

PYBIND11_MODULE(pr_native, m) {
  m.doc() = "physicsRouter native C++ core (OpenMP + optional OpenCL GPU)";

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
      .def_readwrite("net_id", &pr::RectObs::net_id);

  py::class_<pr::NetSpec>(m, "NetSpec")
      .def(py::init<>())
      .def_readwrite("net_id", &pr::NetSpec::net_id)
      .def_readwrite("name", &pr::NetSpec::name)
      .def_readwrite("anchors", &pr::NetSpec::anchors)
      .def_readwrite("priority", &pr::NetSpec::priority)
      .def_readwrite("width_mm", &pr::NetSpec::width_mm)
      .def_readwrite("preferred_layers", &pr::NetSpec::preferred_layers);

  py::class_<pr::RouteConfig>(m, "RouteConfig")
      .def(py::init<>())
      .def_readwrite("x_min", &pr::RouteConfig::x_min)
      .def_readwrite("x_max", &pr::RouteConfig::x_max)
      .def_readwrite("y_min", &pr::RouteConfig::y_min)
      .def_readwrite("y_max", &pr::RouteConfig::y_max)
      .def_readwrite("grid_mm", &pr::RouteConfig::grid_mm)
      .def_readwrite("clearance_mm", &pr::RouteConfig::clearance_mm)
      .def_readwrite("num_layers", &pr::RouteConfig::num_layers)
      .def_readwrite("max_expansions", &pr::RouteConfig::max_expansions)
      .def_readwrite("soft_fallback", &pr::RouteConfig::soft_fallback)
      .def_readwrite("allow_vias", &pr::RouteConfig::allow_vias)
      .def_readwrite("use_gpu", &pr::RouteConfig::use_gpu)
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
      .def_readonly("layer_a", &pr::Via::layer_a)
      .def_readonly("layer_b", &pr::Via::layer_b);

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
        return pr::route_board(nets, cfg, obs, nullptr);
      },
      py::arg("nets"), py::arg("cfg"), py::arg("obstacles") = std::vector<pr::RectObs>{});

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

  m.def("version", []() { return "1.0.0-native"; });
}
