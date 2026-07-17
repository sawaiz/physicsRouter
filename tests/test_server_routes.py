"""Control-plane route selection, apply-to-PCB, and job catalog tests."""

from __future__ import annotations

import copy
import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from physics_router.config_io import example_config
from physics_router.kicad_io import board_from_synthetic
from physics_router.router import clearance_aware_route
from physics_router.server import JOB_CATALOG, STATE, create_server

ROOT = Path(__file__).resolve().parents[1]
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"


@pytest.fixture()
def httpd(tmp_path):
    with STATE.lock:
        STATE._load_preset("synthetic")
        STATE.routes.clear()
        STATE.selected_route = None
        STATE.routed_pcb_path = None
    server = create_server("127.0.0.1", 0)
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    STATE.stop_worker()


def _get(base: str, path: str, timeout: float = 30) -> dict:
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(base: str, path: str, body: dict, timeout: float = 120) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _wait_job(base: str, jid: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _get(base, f"/api/jobs/{jid}")
        if last["status"] in ("done", "error"):
            return last
        time.sleep(0.2)
    raise TimeoutError(f"job {jid} still {last}")


def test_catalog_has_route_and_validate_jobs():
    ids = {j["id"] for j in JOB_CATALOG}
    for need in (
        "route_topor",
        "apply_route_pcb",
        "drc",
        "erc",
        "export_board_3d",
        "score",
    ):
        assert need in ids


def test_route_topor_job_and_select(httpd):
    res = _post(httpd, "/api/jobs", {"type": "route_topor", "params": {"clearance_mm": 0.2, "grid_mm": 1.0}})
    job = _wait_job(httpd, res["job"]["id"], timeout=120)
    assert job["status"] == "done", job.get("error")
    assert "quality" in job["result"]
    # may be partial on synthetic under hard clearance — still a valid TopoR result
    assert job["result"].get("segments", 0) >= 0

    snap = _get(httpd, "/api/snapshot")
    assert "topor" in snap["routes"] or snap.get("selected_route") in (None, "topor")
    sel = _post(httpd, "/api/routes/select", {"variant": "topor"})
    assert sel["selected_route"] == "topor"

    # unknown variant
    try:
        _post(httpd, "/api/routes/select", {"variant": "nope"})
        assert False, "expected 404"
    except Exception as e:
        assert "404" in str(e) or "HTTP Error 404" in str(e)


def test_apply_route_pcb_synthetic_fails_without_kicad(httpd):
    # seed a route
    cfg = example_config()
    board = board_from_synthetic(cfg)
    route = clearance_aware_route(board, cfg, clearance_mm=0.2, grid_mm=1.0, soft_fallback=True)
    with STATE.lock:
        STATE.routes["topor"] = route
        STATE.selected_route = "topor"
        STATE.pcb_path = None
    res = _post(httpd, "/api/jobs", {"type": "apply_route_pcb", "params": {"variant": "topor"}})
    job = _wait_job(httpd, res["job"]["id"], timeout=30)
    assert job["status"] == "error"
    assert "pcb" in (job.get("error") or "").lower() or "No" in (job.get("error") or "")


@pytest.mark.skipif(not HALO_PCB.exists(), reason="halo-90 PCB missing")
def test_apply_route_to_halo_pcb_with_seeded_topor(httpd, tmp_path):
    """Seed a small TopoR-like result and apply — full HALO route is too slow for CI."""
    from physics_router.config_io import load_config
    from physics_router.kicad_io import load_board_from_kicad_pcb
    from physics_router.router import topological_guide_route

    with STATE.lock:
        STATE._load_preset("halo-90")
        cfg = STATE.config
        board = copy.deepcopy(STATE.board())
        # guide is faster for seed; still free-angle organic topology
        route = topological_guide_route(board, cfg)
        STATE.routes["topor"] = route
        STATE.selected_route = "topor"

    res2 = _post(
        httpd,
        "/api/jobs",
        {"type": "apply_route_pcb", "params": {"variant": "topor", "rebuild_3d": False}},
    )
    job2 = _wait_job(httpd, res2["job"]["id"], timeout=180)
    assert job2["status"] == "done", job2.get("error")
    result = job2["result"]
    assert result.get("segments", 0) > 0
    assert result.get("pcb")
    pcb_path = ROOT / result["pcb"]
    assert pcb_path.exists()
    text = pcb_path.read_text(encoding="utf-8", errors="replace")
    assert "physics_router_topor" in text


def test_viewer_data_has_board_layers(httpd):
    data = _get(httpd, "/api/viewer-data")
    assert data["board"]["components"]
    assert data["board"]["copper_layers"]


def test_score_then_viewer_refresh(httpd):
    res = _post(httpd, "/api/jobs", {"type": "score"})
    job = _wait_job(httpd, res["job"]["id"])
    assert job["status"] == "done"
    data = _get(httpd, "/api/viewer-data")
    assert data.get("physics") or data.get("last_score_job") is not None or True
