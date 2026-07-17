"""Control-plane API smoke tests (no long placement/route)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from physics_router.server import STATE, create_server, list_presets


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


def _post(base: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as r:
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
