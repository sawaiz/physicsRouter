"""Local control-plane HTTP server: config, jobs, progress, results, static UI.

Run via::

    physics-router serve --port 8765

Stdlib only (no Flask). Serves ``viewer/`` and JSON APIs under ``/api/``.
"""

from __future__ import annotations

import copy
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from physics_router import __version__
from physics_router.compare import compare_metrics, load_route_metrics, write_comparison_markdown
from physics_router.config_io import example_config, load_config, save_config
from physics_router.dashboard import write_dashboard
from physics_router.design_rules import load_design_rules
from physics_router.dsn_export import export_dsn, write_freerouting_readme
from physics_router.kicad_io import board_from_synthetic, load_board_from_kicad_pcb
from physics_router.models import PlacementConfig
from physics_router.openems_export import export_openems_bundle, geometry_from_board
from physics_router.physics import (
    GeometricSpiceProxy,
    OpenEMSBackend,
    apply_simulation_scores,
    geometric_score,
)
from physics_router.placement import apply_positions, optimize_placement, result_to_dict
from physics_router.router import clearance_aware_route, topological_guide_route
from physics_router.routing_strategies import multilayer_route, pre_route_analysis
from physics_router.viewer_export import build_viewer_payload, write_viewer_data

ROOT = Path(__file__).resolve().parents[2]
VIEWER_DIR = ROOT / "viewer"
WORK_DIR = ROOT / "viewer" / "runs"
EXAMPLES = ROOT / "examples"
CI_SCRIPT = ROOT / "scripts" / "ci_regression.py"
HALO_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
HALO_CFG = ROOT / "examples/halo-90/placement_config.yaml"


# ---------------------------------------------------------------------------
# Job model
# ---------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    type: str
    status: str = "pending"  # pending|running|done|error|cancelled
    progress: float = 0.0
    stage: str = "queued"
    log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, full: bool = True) -> dict[str, Any]:
        d = {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "progress": round(self.progress, 1),
            "stage": self.stage,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "params": self.params,
            "error": self.error,
            "log_tail": self.log[-40:],
            "log_len": len(self.log),
        }
        if full:
            d["log"] = self.log
            d["result"] = self.result
        else:
            # Compact result summary for list view
            if self.result:
                d["result_summary"] = {
                    k: self.result[k]
                    for k in (
                        "score_total",
                        "total_length_mm",
                        "via_count",
                        "clearance_violations",
                        "passed",
                        "failed",
                        "candidates",
                        "notes",
                    )
                    if k in self.result
                }
        return d


class AppState:
    """Mutable session: config, board, routes, jobs."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self.job_order: list[str] = []
        self.queue: queue.Queue[str] = queue.Queue()
        self.config: PlacementConfig = example_config()
        self.config_text: str = ""
        self.preset: str = "synthetic"
        self.board_source: str = "synthetic"  # synthetic | path
        self.pcb_path: str | None = None
        self.routes: dict[str, Any] = {}  # name -> RouteResult-like or RouteResult
        self.last_score: dict[str, Any] | None = None
        self.last_placement: dict[str, Any] | None = None
        self.last_comparison: dict[str, Any] | None = None
        self.viewer_payload: dict[str, Any] | None = None
        self._board = None
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        # Prefer HALO-90 when the open test project is present
        if HALO_CFG.exists() and HALO_PCB.exists():
            self._load_preset("halo-90")
        elif HALO_CFG.exists():
            self._load_preset("halo-90")
        else:
            self._load_preset("synthetic")

    def log(self, job: Job, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        job.log.append(f"[{ts}] {msg}")

    def set_progress(self, job: Job, progress: float, stage: str | None = None) -> None:
        job.progress = max(0.0, min(100.0, float(progress)))
        if stage is not None:
            job.stage = stage

    def _config_to_yaml(self) -> str:
        import yaml

        return yaml.safe_dump(self.config.model_dump(mode="json"), sort_keys=False, default_flow_style=False)

    def _load_preset(self, name: str) -> None:
        if name == "halo-90" and HALO_CFG.exists():
            self.config = load_config(HALO_CFG)
            self.preset = "halo-90"
            self.pcb_path = str(HALO_PCB) if HALO_PCB.exists() else None
            self.board_source = "pcb" if self.pcb_path else "synthetic"
        elif name == "demo" and (EXAMPLES / "placement_config.yaml").exists():
            self.config = load_config(EXAMPLES / "placement_config.yaml")
            self.preset = "demo"
            self.pcb_path = None
            self.board_source = "synthetic"
        else:
            self.config = example_config()
            self.preset = "synthetic"
            self.pcb_path = None
            self.board_source = "synthetic"
        self.config_text = self._config_to_yaml()
        self._board = self._build_board()
        self.routes = {}
        self.last_score = None
        self.last_placement = None
        self._refresh_viewer()

    def _build_board(self):
        if self.board_source == "pcb" and self.pcb_path and Path(self.pcb_path).exists():
            return load_board_from_kicad_pcb(self.pcb_path, self.config)
        return board_from_synthetic(self.config)

    def board(self):
        if self._board is None:
            self._board = self._build_board()
        return self._board

    def reload_board(self) -> None:
        self._board = self._build_board()

    def _refresh_viewer(self) -> None:
        from physics_router.router import RouteResult, RouteSegment, Via

        board = self.board()
        route_objs: dict[str, RouteResult] = {}
        for name, r in self.routes.items():
            if isinstance(r, RouteResult):
                route_objs[name] = r
            elif isinstance(r, dict):
                segs = [
                    RouteSegment(
                        x1=s["x1"],
                        y1=s["y1"],
                        x2=s["x2"],
                        y2=s["y2"],
                        layer=s.get("layer", "F.Cu"),
                        net=s.get("net", ""),
                        width_mm=s.get("width_mm", 0.25),
                    )
                    for s in r.get("segments") or []
                ]
                vias = [
                    Via(
                        x=v["x"],
                        y=v["y"],
                        net=v.get("net", ""),
                        size_mm=v.get("size_mm", 0.8),
                        drill_mm=v.get("drill_mm", 0.4),
                    )
                    for v in r.get("vias") or []
                ]
                route_objs[name] = RouteResult(
                    segments=segs,
                    vias=vias,
                    via_count=int(r.get("via_count") or len(vias)),
                    total_length_mm=float(r.get("total_length_mm") or 0),
                    unrouted_nets=list(r.get("unrouted_nets") or []),
                    clearance_violations=int(r.get("clearance_violations") or 0),
                    notes=list(r.get("notes") or []),
                )
        payload = build_viewer_payload(
            board,
            self.config,
            routes=route_objs or None,
            comparison=self.last_comparison,
            include_score=True,
        )
        payload["config"] = self.config.model_dump(mode="json")
        payload["preset"] = self.preset
        payload["session"] = {
            "board_source": self.board_source,
            "pcb_path": self.pcb_path,
            "components": len(board.components),
            "nets": len(board.nets),
            "movable": len(board.movable_refs()),
            "locked": sum(1 for c in board.components.values() if c.locked),
            "copper_layers": list(board.copper_layers),
        }
        if self.last_score:
            payload["physics"] = payload.get("physics") or {}
            payload["last_score_job"] = self.last_score
        self.viewer_payload = payload
        try:
            write_viewer_data(payload, VIEWER_DIR / "viewer_data.json")
        except OSError:
            pass

    def start_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="pr-jobs", daemon=True)
        self._worker.start()

    def stop_worker(self) -> None:
        self._stop.set()

    def enqueue(self, job_type: str, params: dict[str, Any] | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], type=job_type, params=params or {})
        with self.lock:
            self.jobs[job.id] = job
            self.job_order.append(job.id)
            # Keep last 100
            if len(self.job_order) > 100:
                old = self.job_order.pop(0)
                self.jobs.pop(old, None)
        self.queue.put(job.id)
        self.start_worker()
        return job

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                jid = self.queue.get(timeout=0.4)
            except queue.Empty:
                continue
            job = self.jobs.get(jid)
            if not job or job.status == "cancelled":
                continue
            job.status = "running"
            job.started_at = time.time()
            job.stage = "starting"
            try:
                result = self._run_job(job)
                job.result = result
                job.status = "done"
                job.progress = 100.0
                job.stage = "complete"
            except Exception as e:
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                job.stage = "error"
                self.log(job, traceback.format_exc()[-2000:])
            finally:
                job.ended_at = time.time()
                self.queue.task_done()

    def _run_job(self, job: Job) -> dict[str, Any]:
        t = job.type
        runners: dict[str, Callable[[Job], dict[str, Any]]] = {
            "score": self._job_score,
            "place": self._job_place,
            "route_guide": self._job_route_guide,
            "route_clearance": self._job_route_clearance,
            "pre_route": self._job_pre_route,
            "spice": self._job_spice,
            "openems": self._job_openems,
            "export_dsn": self._job_export_dsn,
            "export_step": self._job_export_step,
            "drc": self._job_drc,
            "tests": self._job_tests,
            "ci_regression": self._job_ci,
            "pipeline": self._job_pipeline,
            "rebuild_viewer": self._job_rebuild_viewer,
            "compare_routes": self._job_compare,
        }
        if t not in runners:
            raise ValueError(f"Unknown job type: {t}")
        return runners[t](job)

    # --- individual jobs ---

    def _job_score(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 5, "loading board")
        self.log(job, "Building board from session config…")
        with self.lock:
            self.reload_board()
            board = copy.deepcopy(self.board())
            cfg = self.config
        self.set_progress(job, 25, "geometric score")
        self.log(job, "Computing geometric multi-objective score…")
        sb = geometric_score(board, cfg)
        self.set_progress(job, 55, "spice / openems proxies")
        self.log(job, "Applying Ngspice + OpenEMS proxies…")
        sb = apply_simulation_scores(
            board,
            cfg,
            sb,
            spice=GeometricSpiceProxy() if cfg.use_spice else None,
            openems=OpenEMSBackend() if cfg.use_openems else None,
        )
        self.set_progress(job, 90, "refresh viewer")
        out = {
            "score": sb.as_dict(),
            "score_total": sb.total,
            "notes": list(sb.notes),
            "weights": cfg.physics.model_dump(mode="json"),
        }
        with self.lock:
            self.last_score = out
            self._refresh_viewer()
        self.log(job, f"Total cost = {sb.total:.3f}")
        return out

    def _job_place(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 5, "loading board")
        with self.lock:
            self.reload_board()
            board = copy.deepcopy(self.board())
            cfg = copy.deepcopy(self.config)
        # Allow param overrides for SA
        p = job.params
        if "num_candidates" in p:
            cfg.num_candidates = int(p["num_candidates"])
        if "sa_iterations" in p:
            cfg.sa_iterations = int(p["sa_iterations"])
        if "random_seed" in p:
            cfg.random_seed = int(p["random_seed"])
        movable = board.movable_refs()
        self.log(
            job,
            f"Placing {len(movable)} unlocked / {len(board.components)} total "
            f"({cfg.num_candidates} candidates × {cfg.sa_iterations} SA iters)…",
        )
        n_cand = cfg.num_candidates

        # Progress: approximate by wrapping optimize in stages via reduced callback
        # We reimplement light progress by running optimize and updating mid-way notes
        self.set_progress(job, 10, f"SA candidates 0/{n_cand}")

        # Monkey-patch style progress via custom loop duplicate would be heavy;
        # report coarse progress while optimize runs in-thread.
        def run():
            return optimize_placement(board, cfg)

        # Run placement (blocking) with fake progress ticks in parallel
        done = {"r": None, "err": None}

        def target():
            try:
                done["r"] = run()
            except Exception as e:
                done["err"] = e

        th = threading.Thread(target=target, daemon=True)
        th.start()
        t0 = time.time()
        # Heuristic duration estimate
        est = max(2.0, n_cand * cfg.sa_iterations * 0.0008 * max(1, len(movable)))
        while th.is_alive():
            elapsed = time.time() - t0
            frac = min(0.92, elapsed / est)
            self.set_progress(job, 10 + 80 * frac, f"SA optimizing… {int(frac * 100)}%")
            th.join(timeout=0.25)
        if done["err"]:
            raise done["err"]
        result = done["r"]
        self.set_progress(job, 95, "saving placement")
        rd = result_to_dict(result)
        # Persist positions onto session board
        with self.lock:
            apply_positions(self.board(), result.best.positions)
            self.last_placement = rd
            self.last_score = {
                "score": result.best.score.as_dict(),
                "score_total": result.best.score.total,
                "notes": list(result.best.score.notes),
            }
            self._refresh_viewer()
            # write run artifact
            outp = WORK_DIR / f"place_{job.id}.json"
            outp.write_text(json.dumps(rd, indent=2) + "\n", encoding="utf-8")
        self.log(job, f"Best candidate #{result.best.candidate_id} score={result.best.score.total:.3f}")
        return {
            "best_candidate_id": result.best.candidate_id,
            "score_total": result.best.score.total,
            "candidates": len(result.candidates),
            "score": result.best.score.as_dict(),
            "notes": list(result.best.score.notes),
            "artifact": str(outp.relative_to(ROOT)),
            "movable": len(movable),
        }

    def _job_route_guide(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 10, "guide route")
        self.log(job, "TopoR free-angle guide routing…")
        with self.lock:
            board = copy.deepcopy(self.board())
            cfg = self.config
        route = topological_guide_route(board, cfg)
        self.set_progress(job, 85, "store")
        d = route.to_dict()
        with self.lock:
            self.routes["guide"] = route
            self._refresh_viewer()
            outp = WORK_DIR / f"route_guide_{job.id}.json"
            outp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        self.log(job, f"Guide: {route.total_length_mm:.2f} mm, {len(route.segments)} segs")
        return {
            "total_length_mm": route.total_length_mm,
            "via_count": route.via_count,
            "clearance_violations": route.clearance_violations,
            "segments": len(route.segments),
            "unrouted": route.unrouted_nets,
            "artifact": str(outp.relative_to(ROOT)),
            "route": d,
        }

    def _job_route_clearance(self, job: Job) -> dict[str, Any]:
        p = job.params
        clearance = float(p.get("clearance_mm", 0.2))
        grid = float(p.get("grid_mm", 0.5))
        self.set_progress(job, 8, "clearance route")
        self.log(job, f"Clearance-aware TopoR (clearance={clearance} mm, grid={grid} mm)…")
        with self.lock:
            board = copy.deepcopy(self.board())
            cfg = self.config
            pcb = self.pcb_path
        rules = load_design_rules(pcb) if pcb and Path(pcb).exists() else None

        done: dict[str, Any] = {"r": None, "err": None}

        def target():
            try:
                if rules is not None:
                    done["r"] = multilayer_route(
                        board, cfg, rules, grid_mm=grid, clearance_mm=clearance
                    )
                else:
                    done["r"] = clearance_aware_route(
                        board, cfg, clearance_mm=clearance, grid_mm=grid
                    )
            except Exception as e:
                done["err"] = e

        th = threading.Thread(target=target, daemon=True)
        th.start()
        t0 = time.time()
        while th.is_alive():
            elapsed = time.time() - t0
            frac = min(0.9, elapsed / 45.0)
            self.set_progress(job, 10 + 80 * frac, f"routing… {int(frac * 100)}%")
            th.join(timeout=0.3)
        if done["err"]:
            raise done["err"]
        route = done["r"]
        d = route.to_dict()
        with self.lock:
            self.routes["topor"] = route
            self._refresh_viewer()
            outp = WORK_DIR / f"route_topor_{job.id}.json"
            outp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        self.log(
            job,
            f"TopoR: {route.total_length_mm:.2f} mm, vias={route.via_count}, "
            f"viol={route.clearance_violations}",
        )
        return {
            "total_length_mm": route.total_length_mm,
            "via_count": route.via_count,
            "clearance_violations": route.clearance_violations,
            "segments": len(route.segments),
            "unrouted": route.unrouted_nets,
            "notes": route.notes,
            "artifact": str(outp.relative_to(ROOT)),
            "route": d,
        }

    def _job_pre_route(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 30, "pre-route analysis")
        with self.lock:
            board = self.board()
            cfg = self.config
            pcb = self.pcb_path
        rules = load_design_rules(pcb) if pcb and Path(pcb).exists() else None
        report = pre_route_analysis(board, cfg, rules)
        self.set_progress(job, 100, "done")
        out = report.to_dict() if hasattr(report, "to_dict") else {"report": report}
        self.log(job, f"Pre-route: density={out.get('estimated_density_pins_per_cm2', '—')}")
        return out

    def _job_spice(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 20, "spice proxy")
        self.log(job, "Running GeometricSpiceProxy / optional ngspice…")
        with self.lock:
            board = copy.deepcopy(self.board())
            cfg = self.config
        sb = geometric_score(board, cfg)
        spice = GeometricSpiceProxy()
        sb = apply_simulation_scores(board, cfg, sb, spice=spice, openems=None)
        self.set_progress(job, 100, "done")
        notes = [n for n in sb.notes if "spice" in n.lower() or "ngspice" in n.lower() or "L≈" in n or "induct" in n.lower()]
        out = {
            "score_total": sb.total,
            "spice_score": sb.spice_score,
            "score": sb.as_dict(),
            "notes": list(sb.notes),
            "spice_notes": notes or list(sb.notes)[:8],
        }
        with self.lock:
            self.last_score = out
            self._refresh_viewer()
        self.log(job, f"spice_score={sb.spice_score:.3f}")
        return out

    def _job_openems(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 15, "openems geometry")
        self.log(job, "Building OpenEMS mesh / geometry export…")
        out_dir = WORK_DIR / f"openems_{job.id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            board = copy.deepcopy(self.board())
            cfg = self.config
        self.set_progress(job, 40, "export bundle")
        # Prefer existing routes so export does not re-route internally
        from physics_router.router import RouteResult

        with self.lock:
            route_obj = self.routes.get("topor") or self.routes.get("guide")
        if route_obj is None:
            route_obj = RouteResult()
        paths = export_openems_bundle(out_dir, board=board, routes=route_obj, config=cfg)
        self.set_progress(job, 70, "openems score proxy")
        sb = geometric_score(board, cfg)
        sb = apply_simulation_scores(board, cfg, sb, spice=None, openems=OpenEMSBackend())
        # Attach EMI geometry for viewer (pad boxes only — avoid re-routing)
        try:
            prims = geometry_from_board(board, routes=None, config=None)
            geom = {
                "primitives": [
                    {
                        "kind": p.kind,
                        "layer": p.layer,
                        "net": p.net,
                        "cx_mm": p.cx,
                        "cy_mm": p.cy,
                        "cz_mm": p.cz,
                        "w_mm": p.w,
                        "h_mm": p.h,
                        "t_mm": p.t,
                    }
                    for p in prims
                    if p.kind == "box"
                ]
            }
            geom_path = VIEWER_DIR / "emi_geometry.json"
            geom_path.write_text(json.dumps(geom, indent=2) + "\n", encoding="utf-8")
            with self.lock:
                if self.viewer_payload is not None:
                    self.viewer_payload["emi_geometry"] = geom
                    self.viewer_payload.setdefault("assets", {})["emi_geometry"] = "emi_geometry.json"
        except Exception as e:
            self.log(job, f"EMI geometry attach skipped: {e}")
        with self.lock:
            self.last_score = {
                "score": sb.as_dict(),
                "score_total": sb.total,
                "notes": list(sb.notes),
            }
            self._refresh_viewer()
        self.set_progress(job, 100, "done")
        self.log(job, f"OpenEMS export → {out_dir}")
        file_map = {}
        for k, v in (paths or {}).items():
            try:
                file_map[k] = str(Path(v).relative_to(ROOT))
            except Exception:
                file_map[k] = str(v)
        return {
            "score_total": sb.total,
            "openems_score": sb.openems_score,
            "export_dir": str(out_dir.relative_to(ROOT)),
            "files": file_map,
            "notes": list(sb.notes),
            "score": sb.as_dict(),
        }

    def _job_export_dsn(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 20, "export DSN")
        out = WORK_DIR / f"board_{job.id}.dsn"
        with self.lock:
            board = self.board()
            cfg = self.config
            pcb = self.pcb_path
        rules = load_design_rules(pcb) if pcb and Path(pcb).exists() else None
        path = export_dsn(board, out, config=cfg, rules=rules)
        write_freerouting_readme(out.parent)
        self.log(job, f"Wrote {path}")
        return {"dsn": str(Path(path).relative_to(ROOT)), "readme": str((out.parent / "FREEROUTING.md").relative_to(ROOT))}

    def _job_export_step(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 10, "export STEP")
        with self.lock:
            pcb = self.pcb_path
        if not pcb or not Path(pcb).exists():
            self.log(job, "No KiCad PCB in session — STEP needs a real .kicad_pcb")
            return {"skipped": True, "reason": "no pcb path (use halo-90 preset or set pcb)"}
        from physics_router.kicad_tools import export_step

        out_dir = WORK_DIR / f"step_{job.id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        self.set_progress(job, 40, "kicad-cli")
        path = export_step(Path(pcb), out_dir / "board_sim.step")
        self.set_progress(job, 100, "done")
        if path:
            return {"step": str(Path(path).relative_to(ROOT)) if Path(path).is_absolute() else str(path)}
        return {"skipped": True, "reason": "kicad-cli STEP failed or unavailable"}

    def _job_drc(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 10, "DRC")
        with self.lock:
            pcb = self.pcb_path
        if not pcb or not Path(pcb).exists():
            self.log(job, "No PCB — cannot run official KiCad DRC")
            return {"skipped": True, "reason": "no pcb path"}
        from physics_router.kicad_tools import run_drc

        out_dir = WORK_DIR / f"drc_{job.id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_json = out_dir / "drc.json"
        self.set_progress(job, 40, "kicad-cli pcb drc")
        report = run_drc(Path(pcb), out_json)
        self.set_progress(job, 100, "done")
        summary = report.to_dict() if hasattr(report, "to_dict") else {"result": str(report)}
        self.log(job, f"DRC summary: {summary}")
        return summary

    def _job_tests(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 5, "pytest")
        patterns = job.params.get("patterns") or []
        args = [sys.executable, "-m", "pytest", "-q", "--tb=line", str(ROOT / "tests")]
        if patterns:
            # pytest -k expression (not a path)
            expr = " or ".join(str(p) for p in patterns)
            args.extend(["-k", expr])
        self.log(job, " ".join(args))
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            args,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        lines: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            lines.append(line)
            self.log(job, line)
            # crude progress from pytest dots
            if re.search(r"\d+ passed", line):
                self.set_progress(job, 95, "finishing")
            else:
                self.set_progress(job, min(90, 10 + len(lines) * 2), "pytest running")
        rc = proc.wait()
        text = "\n".join(lines)
        passed = failed = 0
        m = re.search(r"(\d+) passed", text)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", text)
        if m:
            failed = int(m.group(1))
        self.set_progress(job, 100, "done")
        return {
            "returncode": rc,
            "passed": passed,
            "failed": failed,
            "output": text[-8000:],
            "ok": rc == 0,
        }

    def _job_ci(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 15, "ci_regression")
        self.log(job, "Running score regression baselines…")
        args = [sys.executable, str(CI_SCRIPT)]
        if job.params.get("update_baselines"):
            args.append("--update-baselines")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, env=env, timeout=600)
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.splitlines()[-50:]:
            self.log(job, line)
        self.set_progress(job, 100, "done")
        return {"returncode": proc.returncode, "output": out[-6000:], "ok": proc.returncode == 0}

    def _job_rebuild_viewer(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 30, "rebuild")
        with self.lock:
            self._refresh_viewer()
            if self.viewer_payload and self.last_score:
                write_dashboard(
                    VIEWER_DIR / "dashboard.html",
                    self.viewer_payload.get("physics") or self.last_score,
                    title=f"Physics budget — {self.preset}",
                    board_meta=self.viewer_payload.get("session") or {},
                    routes=self.viewer_payload.get("routes"),
                    comparison=self.last_comparison,
                    viewer_url="index.html",
                )
        self.set_progress(job, 100, "done")
        return {"viewer_data": "viewer/viewer_data.json", "dashboard": "viewer/dashboard.html"}

    def _job_compare(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 20, "compare")
        with self.lock:
            topor = self.routes.get("topor") or self.routes.get("guide")
        if topor is None:
            raise RuntimeError("No route in session — run route_guide or route_clearance first")
        from physics_router.router import RouteResult

        if isinstance(topor, RouteResult):
            td = topor.to_dict()
        else:
            td = topor
        # Write temp metrics
        tmp = WORK_DIR / f"cmp_topor_{job.id}.json"
        tmp.write_text(json.dumps(td, indent=2) + "\n", encoding="utf-8")
        baseline = None
        if job.params.get("baseline_json") and Path(job.params["baseline_json"]).exists():
            baseline = load_route_metrics(job.params["baseline_json"])
        cmp = compare_metrics(load_route_metrics(tmp), baseline)
        with self.lock:
            self.last_comparison = cmp
            self._refresh_viewer()
            write_comparison_markdown(cmp, WORK_DIR / f"comparison_{job.id}.md")
        self.set_progress(job, 100, "done")
        return cmp

    def _job_pipeline(self, job: Job) -> dict[str, Any]:
        """Score → place → guide → clearance → spice → openems → rebuild."""
        steps = job.params.get("steps") or [
            "score",
            "place",
            "route_guide",
            "route_clearance",
            "spice",
            "openems",
            "rebuild_viewer",
        ]
        results: dict[str, Any] = {}
        n = len(steps)
        for i, step in enumerate(steps):
            base = 100.0 * i / n
            self.set_progress(job, base, f"pipeline: {step}")
            self.log(job, f"── pipeline step {i + 1}/{n}: {step}")
            sub = Job(id=f"{job.id}-{step}", type=step, params=job.params.get(step + "_params") or {})
            # Run sub-job inline sharing progress band
            try:
                # Temporarily nest by calling runner with progress remap
                child_result = self._run_job(sub)
                results[step] = {"ok": True, "result": child_result, "log_tail": sub.log[-10:]}
                self.log(job, f"✓ {step}")
            except Exception as e:
                results[step] = {"ok": False, "error": str(e)}
                self.log(job, f"✗ {step}: {e}")
                if job.params.get("stop_on_error", True):
                    raise
            self.set_progress(job, 100.0 * (i + 1) / n, f"done {step}")
        return {"steps": results}

    # --- API helpers ---

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            board = self.board()
            jobs = [self.jobs[i].to_dict(full=False) for i in reversed(self.job_order[-50:])]
            return {
                "version": __version__,
                "preset": self.preset,
                "board_source": self.board_source,
                "pcb_path": self.pcb_path,
                "session": {
                    "components": len(board.components),
                    "nets": len(board.nets),
                    "movable": len(board.movable_refs()),
                    "locked": sum(1 for c in board.components.values() if c.locked),
                    "copper_layers": list(board.copper_layers),
                    "width_mm": board.width_mm,
                    "height_mm": board.height_mm,
                    "project_name": self.config.project_name,
                },
                "config": self.config.model_dump(mode="json"),
                "config_text": self.config_text,
                "routes": {
                    k: (
                        v.to_dict()
                        if hasattr(v, "to_dict")
                        else {
                            "total_length_mm": v.get("total_length_mm"),
                            "via_count": v.get("via_count"),
                            "clearance_violations": v.get("clearance_violations"),
                            "segments": len(v.get("segments") or []),
                        }
                    )
                    for k, v in self.routes.items()
                },
                "last_score": self.last_score,
                "last_placement": {
                    "best_candidate_id": (self.last_placement or {}).get("best_candidate_id"),
                    "best_score": (self.last_placement or {}).get("best_score"),
                }
                if self.last_placement
                else None,
                "last_comparison": self.last_comparison,
                "jobs": jobs,
                "job_types": JOB_CATALOG,
                "presets": list_presets(),
            }


def list_presets() -> list[dict[str, Any]]:
    presets: list[dict[str, Any]] = []
    if HALO_CFG.exists():
        presets.append(
            {
                "id": "halo-90",
                "label": "HALO-90 wearable (default)",
                "has_pcb": HALO_PCB.exists(),
                "config": str(HALO_CFG.relative_to(ROOT)),
                "pcb": str(HALO_PCB.relative_to(ROOT)) if HALO_PCB.exists() else None,
            }
        )
    presets.append(
        {
            "id": "synthetic",
            "label": "Synthetic demo_buck",
            "has_pcb": False,
            "config": "built-in",
        }
    )
    if (EXAMPLES / "placement_config.yaml").exists():
        presets.append(
            {
                "id": "demo",
                "label": "examples/placement_config.yaml",
                "has_pcb": False,
                "config": str((EXAMPLES / "placement_config.yaml").relative_to(ROOT)),
            }
        )
    return presets


JOB_CATALOG = [
    {
        "id": "score",
        "label": "Physics score",
        "group": "analysis",
        "description": "Multi-objective geometric score + spice/OpenEMS proxies",
    },
    {
        "id": "place",
        "label": "Place (SA)",
        "group": "place",
        "description": "Multi-candidate simulated annealing on unlocked components",
    },
    {
        "id": "pre_route",
        "label": "Pre-route analysis",
        "group": "route",
        "description": "Density, layers, via budget, escape hints",
    },
    {
        "id": "route_guide",
        "label": "Route guide (free-angle)",
        "group": "route",
        "description": "TopoR-style topological guide without grid search",
    },
    {
        "id": "route_clearance",
        "label": "Route clearance (TopoR)",
        "group": "route",
        "description": "Clearance-aware free-angle + rubberband cleanup",
    },
    {
        "id": "spice",
        "label": "Spice / PI proxy",
        "group": "sim",
        "description": "GeometricSpiceProxy + optional ngspice rail check",
    },
    {
        "id": "openems",
        "label": "OpenEMS export + EMI proxy",
        "group": "sim",
        "description": "Mesh/geometry bundle + OpenEMS score proxy",
    },
    {
        "id": "export_dsn",
        "label": "Export Specctra DSN",
        "group": "export",
        "description": "For FreeRouting baseline comparison",
    },
    {
        "id": "export_step",
        "label": "Export STEP (KiCad)",
        "group": "export",
        "description": "Simulation STEP with copper/mask/silk (needs PCB)",
    },
    {
        "id": "drc",
        "label": "KiCad DRC",
        "group": "validate",
        "description": "Official kicad-cli pcb drc oracle (needs PCB)",
    },
    {
        "id": "compare_routes",
        "label": "Compare routes",
        "group": "validate",
        "description": "TopoR metrics vs optional FreeRouting baseline",
    },
    {
        "id": "tests",
        "label": "Unit tests (pytest)",
        "group": "test",
        "description": "Run the physicsRouter test suite",
    },
    {
        "id": "ci_regression",
        "label": "CI score regression",
        "group": "test",
        "description": "Synthetic (+ HALO) baseline check",
    },
    {
        "id": "rebuild_viewer",
        "label": "Rebuild viewer data",
        "group": "export",
        "description": "Refresh viewer_data.json + static dashboard",
    },
    {
        "id": "pipeline",
        "label": "Full pipeline",
        "group": "pipeline",
        "description": "Score → place → route → sims → rebuild (progress per step)",
    },
]


STATE = AppState()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(VIEWER_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Quieter default
        if "/api/" in (args[0] if args else ""):
            return
        super().log_message(fmt, *args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            self._api_get(path, parse_qs(parsed.query))
            return
        if path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._json(404, {"error": "not found"})
            return
        self._api_post(parsed.path)

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._json(404, {"error": "not found"})
            return
        self._api_put(parsed.path)

    def _api_get(self, path: str, qs: dict[str, list[str]]) -> None:
        try:
            if path == "/api/health":
                return self._json(200, {"ok": True, "version": __version__})
            if path == "/api/snapshot":
                return self._json(200, STATE.snapshot())
            if path == "/api/presets":
                return self._json(200, {"presets": list_presets()})
            if path == "/api/config":
                with STATE.lock:
                    return self._json(
                        200,
                        {
                            "preset": STATE.preset,
                            "config": STATE.config.model_dump(mode="json"),
                            "config_text": STATE.config_text,
                            "pcb_path": STATE.pcb_path,
                            "board_source": STATE.board_source,
                        },
                    )
            if path == "/api/jobs":
                with STATE.lock:
                    jobs = [STATE.jobs[i].to_dict(full=False) for i in reversed(STATE.job_order[-50:])]
                return self._json(200, {"jobs": jobs})
            if path.startswith("/api/jobs/"):
                jid = path.split("/")[3]
                with STATE.lock:
                    job = STATE.jobs.get(jid)
                if not job:
                    return self._json(404, {"error": "job not found"})
                full = qs.get("full", ["1"])[0] != "0"
                return self._json(200, job.to_dict(full=full))
            if path == "/api/viewer-data":
                with STATE.lock:
                    if STATE.viewer_payload is None:
                        STATE._refresh_viewer()
                    return self._json(200, STATE.viewer_payload)
            if path == "/api/catalog":
                return self._json(200, {"jobs": JOB_CATALOG})
            if path == "/api/results":
                # List run artifacts
                files = []
                if WORK_DIR.exists():
                    for p in sorted(WORK_DIR.rglob("*"), key=lambda x: x.stat().st_mtime, reverse=True)[:80]:
                        if p.is_file():
                            files.append(
                                {
                                    "path": str(p.relative_to(ROOT)),
                                    "name": p.name,
                                    "size": p.stat().st_size,
                                    "mtime": p.stat().st_mtime,
                                }
                            )
                return self._json(200, {"files": files})
            return self._json(404, {"error": f"unknown api {path}"})
        except Exception as e:
            return self._json(500, {"error": str(e), "trace": traceback.format_exc()[-1500:]})

    def _api_post(self, path: str) -> None:
        try:
            body = self._read_json()
            if path == "/api/jobs":
                jtype = body.get("type")
                if not jtype:
                    return self._json(400, {"error": "type required"})
                params = body.get("params") or {}
                job = STATE.enqueue(jtype, params)
                return self._json(202, {"job": job.to_dict(full=False)})
            if path == "/api/preset":
                name = body.get("preset") or body.get("id") or "synthetic"
                with STATE.lock:
                    STATE._load_preset(name)
                    snap = STATE.snapshot()
                return self._json(200, snap)
            if path == "/api/config/apply":
                # Apply JSON config object or YAML text
                if "config_text" in body:
                    import yaml

                    data = yaml.safe_load(body["config_text"]) or {}
                    cfg = PlacementConfig.model_validate(data)
                    text = body["config_text"]
                elif "config" in body:
                    cfg = PlacementConfig.model_validate(body["config"])
                    import yaml

                    text = yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False, default_flow_style=False)
                else:
                    return self._json(400, {"error": "config or config_text required"})
                with STATE.lock:
                    STATE.config = cfg
                    STATE.config_text = text
                    if body.get("pcb_path"):
                        STATE.pcb_path = body["pcb_path"]
                        STATE.board_source = "pcb"
                    STATE.reload_board()
                    STATE._refresh_viewer()
                    snap = STATE.snapshot()
                return self._json(200, snap)
            if path == "/api/config/save":
                dest = body.get("path") or str(WORK_DIR / "session_config.yaml")
                with STATE.lock:
                    save_config(STATE.config, dest)
                return self._json(200, {"saved": dest})
            return self._json(404, {"error": f"unknown api {path}"})
        except Exception as e:
            return self._json(500, {"error": str(e), "trace": traceback.format_exc()[-1500:]})

    def _api_put(self, path: str) -> None:
        # alias to apply config
        if path == "/api/config":
            return self._api_post("/api/config/apply")
        return self._json(404, {"error": "not found"})


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    VIEWER_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    STATE.start_worker()
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = create_server(host, port)
    print(f"physicsRouter control plane  v{__version__}")
    print(f"  UI   http://{host}:{port}/")
    print(f"  API  http://{host}:{port}/api/snapshot")
    print("  Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        STATE.stop_worker()
        httpd.server_close()
