"""Tests for zones, net rules, DSN fidelity, SES import, routing policy APIs."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from physics_router.config_io import example_config
from physics_router.dsn_export import export_dsn
from physics_router.kicad_io import load_board_from_kicad_pcb
from physics_router.models import KeepoutRegion, NetClass, NetLabel, PlacementConfig
from physics_router.net_import import _infer_diff_pairs, build_net_labels
from physics_router.pin_access import _package_kind
from physics_router.router import build_obstacle_map
from physics_router.server import STATE, create_server

ROOT = Path(__file__).resolve().parents[1]
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"


@pytest.mark.skipif(not HALO_PCB.exists(), reason="halo missing")
def test_zones_parsed_and_obstacles():
    board = load_board_from_kicad_pcb(HALO_PCB)
    # HALO may or may not have pours; ensure API is stable
    assert isinstance(board.zones, list)
    om = build_obstacle_map(
        board,
        clearance_mm=0.15,
        keepouts=[{"x1": 1.0, "y1": 1.0, "x2": 3.0, "y2": 3.0}],
    )
    assert om.layers
    # Keepout center should be blocked for foreign nets
    assert om.blocked(2.0, 2.0, om.layers[0], "FOREIGN_NET")


def test_diff_pair_inference():
    pairs = _infer_diff_pairs({"USB_DP", "USB_DM", "GND", "VCC"})
    assert pairs.get("USB_DP") == "USB_DM"
    assert pairs.get("USB_DM") == "USB_DP"


def test_netlabel_geometry_fields():
    lab = NetLabel(
        name="CLK",
        net_class=NetClass.CLOCK,
        track_width_mm=0.2,
        clearance_mm=0.15,
        max_length_mm=25.0,
        locked=True,
    )
    cfg = PlacementConfig(nets=[lab], keepouts=[KeepoutRegion(x1=0, y1=0, x2=1, y2=1)])
    assert cfg.net_by_name()["CLK"].track_width_mm == 0.2
    assert len(cfg.keepouts) == 1


@pytest.mark.skipif(not HALO_PCB.exists(), reason="halo missing")
def test_dsn_uses_real_pad_coords(tmp_path: Path):
    board = load_board_from_kicad_pcb(HALO_PCB)
    cfg = example_config()
    out = tmp_path / "board.dsn"
    export_dsn(board, out, config=cfg)
    text = out.read_text(encoding="utf-8")
    assert "(pcb" in text
    assert "(library" in text
    assert "(network" in text
    # pin with non-trivial offsets should appear as numbers, not only grid fallback
    assert '(pin "Pad"' in text or "(pin \"Pad\"" in text


def test_package_kind_bga_qfn():
    class C:
        footprint = "Package_BGA:BGA-64"
        pads = [{"x": 0, "y": 0}] * 64

    assert _package_kind(C()) == "bga"

    class Q:
        footprint = "Package_DFN_QFN:QFN-32"
        pads = [{"x": 0, "y": 0}, {"x": 1, "y": 0}] + [{"x": 0.5, "y": 0.5}] * 14

    assert _package_kind(Q()) == "qfn"


def test_ses_import_minimal(tmp_path: Path):
    from physics_router.ses_import import parse_ses_to_route

    ses = tmp_path / "t.ses"
    # Minimal SES-like structure with one wire path (mil coords)
    ses.write_text(
        """
(session board
  (routes
    (network_out
      (net "SIG"
        (wire
          (path Signal_0 10 0 0 100 0)
          (net "SIG")
        )
      )
    )
  )
)
""",
        encoding="utf-8",
    )
    r = parse_ses_to_route(ses, copper_layers=["F.Cu", "B.Cu"])
    assert len(r.segments) >= 1
    assert r.segments[0].net == "SIG"
    assert r.segments[0].layer == "F.Cu"


@pytest.fixture()
def httpd():
    with STATE.lock:
        STATE._load_preset("synthetic")
        STATE.locked_nets = []
        STATE.keepouts = []
        STATE.reroute_nets = []
    server = create_server("127.0.0.1", 0)
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    STATE.stop_worker()


def _post(base: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode())


def test_routing_policy_api(httpd):
    out = _post(
        httpd,
        "/api/routing/policy",
        {
            "locked_nets": ["VCC", "GND"],
            "reroute_nets": ["SW"],
            "keepouts": [{"x1": 0, "y1": 0, "x2": 2, "y2": 2}],
        },
    )
    assert out["ok"] is True
    assert "VCC" in out["locked_nets"]
    assert out["reroute_nets"] == ["SW"]
    assert len(out["keepouts"]) == 1
    snap = _get(httpd, "/api/snapshot")
    assert "VCC" in snap["locked_nets"]
    assert "net_names" in snap
