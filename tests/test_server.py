"""Control-plane API smoke tests (no long placement/route)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from physics_router.server import STATE, create_server, list_presets

ROOT = Path(__file__).resolve().parents[1]
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"


@pytest.fixture(scope="module")
def httpd():
    # Isolate session state
    with STATE.lock:
        STATE._load_preset("synthetic")
    server = create_server("127.0.0.1", 0)
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    STATE.stop_worker()


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read().decode())


def _post(base: str, path: str, body: dict, timeout: float = 60) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def test_health_and_snapshot(httpd):
    h = _get(httpd, "/api/health")
    assert h["ok"] is True
    assert "native" in h
    assert "available" in h["native"]
    snap = _get(httpd, "/api/snapshot")
    assert "config" in snap
    assert "job_types" in snap
    assert snap["session"]["components"] >= 1


def test_presets_list():
    p = list_presets()
    assert any(x["id"] == "synthetic" for x in p)
    assert any(x["id"] == "custom" and x.get("importable") for x in p)


@pytest.mark.skipif(not HALO_PCB.exists(), reason="halo-90 PCB missing")
def test_board_open_path(httpd):
    out = _post(httpd, "/api/board/open", {"pcb_path": str(HALO_PCB)})
    assert out.get("ok") is True
    assert out["preset"] == "custom"
    assert out["board"]["components"] > 10
    assert out["board"]["nets"] >= 1
    assert Path(out["pcb_path"]).exists()
    snap = _get(httpd, "/api/snapshot")
    assert snap["preset"] == "custom"
    assert snap["session"]["components"] == out["board"]["components"]
    viewer = _get(httpd, "/api/viewer-data")
    assert len(viewer["board"]["components"]) == out["board"]["components"]


@pytest.mark.skipif(not HALO_PCB.exists(), reason="halo-90 PCB missing")
def test_board_import_bytes(httpd):
    text = HALO_PCB.read_text(encoding="utf-8", errors="replace")
    # Use a trimmed-enough header check path via full content
    out = _post(
        httpd,
        "/api/board/import",
        {"pcb_text": text, "filename": "halo_upload.kicad_pcb"},
        timeout=120,
    )
    assert out.get("ok") is True
    assert out["preset"] == "custom"
    assert out["board"]["components"] > 10
    assert "imports" in out["pcb_path"] or "halo_upload" in out["pcb_path"]
    assert Path(out["pcb_path"]).is_file()


def test_board_open_missing_path(httpd):
    try:
        _post(httpd, "/api/board/open", {"pcb_path": "/no/such/board.kicad_pcb"})
        assert False, "expected 500"
    except urllib.error.HTTPError as e:
        assert e.code == 500
        body = json.loads(e.read().decode())
        assert "not found" in body.get("error", "").lower() or "PCB" in body.get("error", "")


def test_score_job_progress(httpd):
    res = _post(httpd, "/api/jobs", {"type": "score"})
    jid = res["job"]["id"]
    # Poll until done
    deadline = time.time() + 60
    last = None
    while time.time() < deadline:
        last = _get(httpd, f"/api/jobs/{jid}")
        if last["status"] in ("done", "error"):
            break
        time.sleep(0.15)
    assert last is not None
    assert last["status"] == "done", last.get("error")
    assert last["progress"] == 100
    assert last["result"]["score_total"] is not None
    assert last["log_len"] >= 1


def test_apply_config(httpd):
    snap = _get(httpd, "/api/config")
    cfg = snap["config"]
    cfg["num_candidates"] = 3
    cfg["sa_iterations"] = 200
    out = _post(httpd, "/api/config/apply", {"config": cfg})
    assert out["config"]["num_candidates"] == 3


def test_viewer_data(httpd):
    data = _get(httpd, "/api/viewer-data")
    assert "board" in data
    assert "components" in data["board"]
