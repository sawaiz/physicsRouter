"""Hardening: keepouts in polish, zones as obstacles, SES, seed merge."""

from __future__ import annotations

from pathlib import Path

import pytest

from physics_router.kicad_io import parse_sexpr, _extract_zones, board_from_synthetic
from physics_router.config_io import example_config
from physics_router.models import KeepoutRegion, BoardModel, Component
from physics_router.router import (
    RouteResult,
    RouteSegment,
    Via,
    build_obstacle_map,
    merge_seed_into_result,
    rubberband_cleanup,
)
from physics_router.elastic import elastic_optimize_route
from physics_router.regeometry import post_connect_regeometry
from physics_router.ses_import import parse_ses_to_route


def test_zone_obstacle_blocks_foreign_net():
    board = BoardModel(
        width_mm=40,
        height_mm=40,
        copper_layers=["F.Cu", "B.Cu"],
        components={},
        nets={"SIG": [("R1", "1"), ("R2", "1")], "GND": [("C1", "1")]},
        zones=[
            {
                "net": "GND",
                "layer": "F.Cu",
                "keepout": False,
                "points": [[5, 5], [25, 5], [25, 25], [5, 25]],
                "filled": True,
            }
        ],
    )
    om = build_obstacle_map(board, clearance_mm=0.15, layers=["F.Cu", "B.Cu"])
    # Center of GND pour blocks foreign net
    assert om.blocked(15.0, 15.0, "F.Cu", "SIG")
    # Same net may pass
    assert not om.blocked(15.0, 15.0, "F.Cu", "GND")
    # Outside pour free
    assert not om.blocked(1.0, 1.0, "F.Cu", "SIG")


def test_extract_zones_from_sexpr():
    text = """
    (kicad_pcb
      (zone (net 1) (net_name "GND") (layer "F.Cu")
        (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))
      )
      (zone (net 0) (net_name "") (layer "B.Cu")
        (keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour allowed) (footprints allowed))
        (polygon (pts (xy 1 1) (xy 3 1) (xy 3 3) (xy 1 3)))
      )
    )
    """
    root = parse_sexpr(text)
    zones = _extract_zones(root, ["F.Cu", "B.Cu"])
    assert len(zones) >= 2
    gnd = [z for z in zones if z.get("net") == "GND"]
    assert gnd and len(gnd[0]["points"]) >= 3
    ko = [z for z in zones if z.get("keepout")]
    assert ko


def test_keepout_survives_rubberband_and_elastic():
    cfg = example_config()
    cfg.keepouts = [KeepoutRegion(x1=8, y1=8, x2=18, y2=18)]
    board = board_from_synthetic(cfg)
    # Segment that skirts the keepout
    route = RouteResult(
        segments=[
            RouteSegment(0, 0, 5, 5, layer="F.Cu", net="SW", width_mm=0.25),
            RouteSegment(5, 5, 22, 22, layer="F.Cu", net="SW", width_mm=0.25),
        ],
        vias=[],
        via_count=0,
    )
    rb = rubberband_cleanup(route, board, cfg, clearance_mm=0.2)
    # After rubberband, keepout region should still block foreign at center
    om = build_obstacle_map(
        board, clearance_mm=0.2, layers=["F.Cu"], keepouts=cfg.keepouts
    )
    assert om.blocked(13, 13, "F.Cu", "OTHER")
    # elastic should accept config without error
    el = elastic_optimize_route(rb, board, clearance_mm=0.2, iterations=4, config=cfg)
    assert el.segments
    geo = post_connect_regeometry(el, board, clearance_mm=0.2, iterations=3, config=cfg)
    assert geo.segments


def test_merge_seed_into_result():
    seed = RouteResult(
        segments=[RouteSegment(0, 0, 1, 0, layer="F.Cu", net="GND", width_mm=0.4)],
        vias=[Via(x=0.5, y=0, net="GND", size_mm=0.6, drill_mm=0.3)],
        via_count=1,
    )
    detail = RouteResult(
        segments=[
            RouteSegment(0, 0, 2, 2, layer="F.Cu", net="GND", width_mm=0.2),  # replaced
            RouteSegment(1, 1, 3, 3, layer="F.Cu", net="SW", width_mm=0.2),
        ],
        vias=[],
        via_count=0,
        unrouted_nets=["SW", "GND"],
    )
    m = merge_seed_into_result(seed, detail)
    assert len([s for s in m.segments if s.net == "GND"]) == 1
    assert m.segments[0].width_mm == 0.4  # seed wins
    assert any(s.net == "SW" for s in m.segments)
    assert "GND" not in m.unrouted_nets
    assert any(v.net == "GND" for v in m.vias)


def test_ses_freerouting_network_out(tmp_path: Path):
    ses = tmp_path / "board.ses"
    ses.write_text(
        """
(session board
  (resolution mil 10)
  (unit mil)
  (routes
    (network_out
      (net "USB_DP"
        (wire
          (path Signal_0 8 0 0 100 0 100 50)
          (net "USB_DP")
        )
        (via "Via[0-1]_0:24:12" 100 50 (net "USB_DP"))
      )
      (net "USB_DM"
        (wire (path Signal_1 8 0 20 100 20))
      )
    )
  )
)
""",
        encoding="utf-8",
    )
    r = parse_ses_to_route(ses, copper_layers=["F.Cu", "B.Cu", "In1.Cu"])
    assert len(r.segments) >= 2
    nets = {s.net for s in r.segments}
    assert "USB_DP" in nets
    assert "USB_DM" in nets
    # mil → mm: 100 mil ≈ 2.54 mm
    assert any(abs(s.x2 - 2.54) < 0.05 or abs(s.x1 - 2.54) < 0.05 for s in r.segments)
    assert r.via_count >= 1
    assert any(v.net == "USB_DP" for v in r.vias)
    # Signal_1 → B.Cu when copper[1]
    assert any(s.layer == "B.Cu" for s in r.segments if s.net == "USB_DM")


def test_ses_mm_resolution(tmp_path: Path):
    ses = tmp_path / "mm.ses"
    ses.write_text(
        """
(session x
  (resolution mm 1)
  (routes
    (wire (path F.Cu 0.25 0 0 5 0) (net "A"))
  )
)
""",
        encoding="utf-8",
    )
    r = parse_ses_to_route(ses, copper_layers=["F.Cu", "B.Cu"])
    assert len(r.segments) == 1
    assert abs(r.segments[0].x2 - 5.0) < 1e-6
    assert r.segments[0].layer == "F.Cu"
