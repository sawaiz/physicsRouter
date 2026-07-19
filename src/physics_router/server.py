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
from physics_router.compare import (
    compare_metrics,
    load_route_metrics,
    write_comparison_markdown,
)
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
from physics_router.routing_strategies import multilayer_route, pre_route_analysis
from physics_router.viewer_export import build_viewer_payload, write_viewer_data

ROOT = Path(__file__).resolve().parents[2]
VIEWER_DIR = ROOT / "viewer"
WORK_DIR = ROOT / "viewer" / "runs"
ASSETS_DIR = ROOT / "viewer" / "assets"
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
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        self.glb_url: str | None = None
        self.step_url: str | None = None
        self.selected_route: str | None = None  # guide | topor | …
        self.routed_pcb_path: str | None = None  # last applied copper PCB
        # JLCPCB fab profile: 2layer_* / 4layer_* / 6layer_* (recommended|capability)
        self.fab_profile: str = "4layer_recommended"
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

        return yaml.safe_dump(
            self.config.model_dump(mode="json"),
            sort_keys=False,
            default_flow_style=False,
        )

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
        self.selected_route = None
        self.routed_pcb_path = None
        self.last_score = None
        self.last_placement = None
        self._discover_assets()
        self._refresh_viewer()
        # Kick off full KiCad 3D (STEP models + mask/silk) if PCB present and GLB missing
        if self.pcb_path and Path(self.pcb_path).exists() and not self.glb_url:
            try:
                self.enqueue("export_board_3d", {"also_step": False})
            except Exception:
                pass

    def _build_board(self):
        if (
            self.board_source == "pcb"
            and self.pcb_path
            and Path(self.pcb_path).exists()
        ):
            return load_board_from_kicad_pcb(self.pcb_path, self.config)
        return board_from_synthetic(self.config)

    def board(self):
        if self._board is None:
            self._board = self._build_board()
        return self._board

    def reload_board(self) -> None:
        self._board = self._build_board()

    def _discover_assets(self) -> None:
        """Point session at viewer/assets/*.glb / *.step if present."""
        self.glb_url = None
        self.step_url = None
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        candidates: list[Path] = []
        if self.preset:
            candidates.append(ASSETS_DIR / f"{self.preset}.glb")
        if self.pcb_path:
            candidates.append(ASSETS_DIR / f"{Path(self.pcb_path).stem}.glb")
        candidates.append(ASSETS_DIR / "board.glb")
        for p in candidates:
            if p.is_file() and p.stat().st_size > 1000:
                self.glb_url = f"/assets/{p.name}"
                break
        for p in [
            ASSETS_DIR / f"{self.preset}_full.step" if self.preset else None,
            ASSETS_DIR / "board_full.step",
        ]:
            if p and p.is_file():
                self.step_url = f"/assets/{p.name}"
                break

    def _refresh_viewer(self) -> None:
        from physics_router.router import CopperArea, RouteResult, RouteSegment, Via

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
                areas = [
                    CopperArea(
                        outline=[tuple(point) for point in area.get("outline") or []],
                        layer=area.get("layer", "F.Cu"),
                        net=area.get("net", ""),
                        clearance_mm=area.get("clearance_mm", 0.2),
                        min_thickness_mm=area.get("min_thickness_mm", 0.25),
                        priority=area.get("priority", 0),
                    )
                    for area in r.get("areas") or []
                ]
                route_objs[name] = RouteResult(
                    segments=segs,
                    vias=vias,
                    areas=areas,
                    via_count=int(r.get("via_count") or len(vias)),
                    total_length_mm=float(r.get("total_length_mm") or 0),
                    unrouted_nets=list(r.get("unrouted_nets") or []),
                    clearance_violations=int(r.get("clearance_violations") or 0),
                    notes=list(r.get("notes") or []),
                )
        self._discover_assets()
        payload = build_viewer_payload(
            board,
            self.config,
            routes=route_objs or None,
            comparison=self.last_comparison,
            include_score=True,
            glb_url=self.glb_url,
            step_url=self.step_url,
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
            "glb": self.glb_url,
            "step": self.step_url,
        }
        payload.setdefault("assets", {})
        payload["assets"]["glb"] = self.glb_url
        payload["assets"]["step"] = self.step_url
        payload["assets"]["detail"] = (
            "KiCad GLB: footprint STEP models + tracks + pads + zones + "
            "inner copper + soldermask + silkscreen"
            if self.glb_url
            else "Run export_board_3d (or wait for auto-export) for full visual PCB"
        )
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
        self._worker = threading.Thread(
            target=self._worker_loop, name="pr-jobs", daemon=True
        )
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
            "route_guide": self._job_route_topor,  # alias → single TopoR path
            "route_clearance": self._job_route_topor,
            "route_topor": self._job_route_topor,
            "improve": self._job_improve,
            "continuous_improve": self._job_improve,
            "pre_route": self._job_pre_route,
            "spice": self._job_spice,
            "openems": self._job_openems,
            "export_dsn": self._job_export_dsn,
            "export_step": self._job_export_step,
            "export_board_3d": self._job_export_board_3d,
            "apply_route_pcb": self._job_apply_route_pcb,
            "drc": self._job_drc,
            "erc": self._job_erc,
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
        self.log(
            job,
            f"Best candidate #{result.best.candidate_id} score={result.best.score.total:.3f}",
        )
        return {
            "best_candidate_id": result.best.candidate_id,
            "score_total": result.best.score.total,
            "candidates": len(result.candidates),
            "score": result.best.score.as_dict(),
            "notes": list(result.best.score.notes),
            "artifact": str(outp.relative_to(ROOT)),
            "movable": len(movable),
        }

    def _publish_live_route(
        self, partial: dict[str, Any], label: str = "topor"
    ) -> None:
        """Push partial route geometry into session so the UI can redraw mid-job."""
        from physics_router.router import CopperArea, RouteResult, RouteSegment, Via

        segs = [
            RouteSegment(
                x1=float(s["x1"]),
                y1=float(s["y1"]),
                x2=float(s["x2"]),
                y2=float(s["y2"]),
                layer=str(s.get("layer", "F.Cu")),
                net=str(s.get("net", "")),
                width_mm=float(s.get("width_mm", 0.25)),
            )
            for s in partial.get("segments") or []
        ]
        vias = [
            Via(
                x=float(v["x"]),
                y=float(v["y"]),
                net=str(v.get("net", "")),
                size_mm=float(v.get("size_mm", 0.8)),
                drill_mm=float(v.get("drill_mm", 0.4)),
                layers=tuple(v.get("layers") or ("F.Cu", "B.Cu")),  # type: ignore[arg-type]
            )
            for v in partial.get("vias") or []
        ]
        areas = [
            CopperArea(
                outline=[tuple(point) for point in area.get("outline") or []],
                layer=area.get("layer", "F.Cu"),
                net=area.get("net", ""),
                clearance_mm=area.get("clearance_mm", 0.2),
                min_thickness_mm=area.get("min_thickness_mm", 0.25),
                priority=area.get("priority", 0),
            )
            for area in partial.get("areas") or []
        ]
        live = RouteResult(
            segments=segs,
            vias=vias,
            areas=areas,
            via_count=int(partial.get("via_count") or len(vias)),
            total_length_mm=float(partial.get("total_length_mm") or 0),
            unrouted_nets=list(partial.get("unrouted_nets") or []),
            clearance_violations=int(partial.get("clearance_violations") or 0),
            notes=["live routing progress…"],
        )
        with self.lock:
            self.routes[label] = live
            self.selected_route = label
            self._refresh_viewer()

    def _job_route_topor(self, job: Job) -> dict[str, Any]:
        """TopoR-style isotropic free-angle route (multi-variant + geometry polish)."""
        from physics_router.routing_strategies import (
            topor_style_route,
        )

        p = job.params
        with self.lock:
            board = copy.deepcopy(self.board())
            cfg = self.config
            pcb_path = self.pcb_path
        from physics_router.design_rules import (
            jlcpcb_design_rules,
            list_jlcpcb_profiles,
            parse_jlc_profile,
        )

        with self.lock:
            fab_profile = str(
                p.get("fab_profile") or self.fab_profile or "4layer_recommended"
            )
            self.fab_profile = fab_profile
        n_layers, aggressive = parse_jlc_profile(fab_profile)
        rules = (
            load_design_rules(pcb_path, manufacturer="JLCPCB", jlc_profile=fab_profile)
            if pcb_path and Path(pcb_path).exists()
            else jlcpcb_design_rules(layers=n_layers, aggressive=aggressive)
        )
        # Default clearance/grid from selected JLC profile floors unless caller overrides
        clearance = float(p.get("clearance_mm", rules.constraints.min_clearance_mm))
        grid = float(p.get("grid_mm", max(0.15, rules.constraints.min_track_width_mm)))
        num_variants = p.get("num_variants")
        if num_variants is not None:
            num_variants = int(num_variants)
        self.set_progress(job, 3, "TopoR isotropic free-angle")
        from physics_router.native_bridge import available as native_ok

        self.log(
            job,
            f"TopoR pipeline: free-angle · clearance={clearance} mm · "
            f"grid={grid} mm · vias=through-hole · variants={num_variants or 'auto'} · "
            f"native_cpp={'on' if native_ok() else 'OFF'} · "
            f"fab={rules.constraints.manufacturer or '—'} "
            f"{rules.constraints.manufacturer_profile or ''}",
        )
        self.log(
            job,
            f"  DRC floors JLC{n_layers}L: track≥{rules.constraints.min_track_width_mm}mm "
            f"clear≥{rules.constraints.min_clearance_mm}mm "
            f"via≥{rules.constraints.min_via_diameter_mm}/"
            f"{rules.constraints.min_via_drill_mm}mm "
            f"edge≥{rules.constraints.min_copper_edge_clearance_mm}mm "
            f"blind/buried={'yes' if rules.constraints.allow_blind_buried_vias else 'no'}",
        )
        # Log profile suggestions / limitations (first lines)
        for prof in list_jlcpcb_profiles():
            if prof["id"] == fab_profile:
                for s in (prof.get("suggestions") or [])[:2]:
                    self.log(job, f"  tip: {s}")
                for lim in (prof.get("limitations") or [])[:2]:
                    self.log(job, f"  limit: {lim}")
                break

        last_pub = {"n": -1}

        def on_progress(
            done_n: int, total: int, name: str, stage: str, detail: dict
        ) -> None:
            frac = 5 + 80 * (done_n / max(total, 1))
            self.set_progress(job, frac, f"TopoR {done_n}/{total} · {name} · {stage}")
            if isinstance(detail, dict):
                if detail.get("variant"):
                    self.log(
                        job,
                        f"  variant {detail.get('index', '?')}: {detail['variant']}",
                    )
                if detail.get("length_mm") is not None:
                    self.log(
                        job,
                        f"  [{done_n}/{total}] {name}: {stage} "
                        f"L={float(detail.get('length_mm') or 0):.2f}mm "
                        f"vias={detail.get('vias', 0)} method={detail.get('method', '')}",
                    )
                partial = detail.get("partial")
                # Publish after each net so the UI can animate copper growth
                if partial and done_n != last_pub["n"] and stage != "routing":
                    try:
                        self._publish_live_route(partial, "topor")
                        last_pub["n"] = done_n
                    except Exception as e:
                        self.log(job, f"  live preview skip: {e}")

        done: dict[str, Any] = {"r": None, "err": None}

        def target() -> None:
            try:
                if rules is not None:
                    done["r"] = multilayer_route(
                        board,
                        cfg,
                        rules,
                        grid_mm=grid,
                        clearance_mm=clearance,
                        num_variants=num_variants,
                        progress_cb=on_progress,
                    )
                else:
                    done["r"] = topor_style_route(
                        board,
                        cfg,
                        None,
                        clearance_mm=clearance,
                        grid_mm=grid,
                        num_variants=num_variants,
                        progress_cb=on_progress,
                    )
            except Exception as e:
                done["err"] = e

        th = threading.Thread(target=target, daemon=True)
        th.start()
        while th.is_alive():
            th.join(timeout=0.25)
        if done["err"]:
            raise done["err"]
        route = done["r"]
        d = route.to_dict()
        with self.lock:
            self.routes = {"topor": route}
            self.selected_route = "topor"
            self._refresh_viewer()
            outp = WORK_DIR / f"route_topor_{job.id}.json"
            outp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        q = route.quality or route.compute_quality()
        self.log(job, f"TopoR done: {q.get('summary')}")
        if q.get("winner"):
            self.log(job, f"  winner variant: {q.get('winner')}")
        for row in (q.get("variants_ranked") or [])[:4]:
            self.log(
                job,
                f"  · {row.get('name')}: grade={row.get('grade')} score={row.get('score')} "
                f"L={row.get('length_mm')} vias={row.get('vias')} unrouted={row.get('unrouted')}",
            )
        for note in (route.notes or [])[-10:]:
            self.log(job, f"  note: {note}")

        auto_apply = bool(p.get("auto_apply", False))  # manual apply from UI by default
        validation: dict[str, Any] = {}
        with self.lock:
            has_pcb = bool(self.pcb_path and Path(self.pcb_path).exists())
        if auto_apply and has_pcb and route.segments:
            self.set_progress(job, 90, "auto-apply + DRC")
            sub = Job(
                id=f"{job.id}-apply",
                type="apply_route_pcb",
                params={
                    "variant": "topor",
                    "rebuild_3d": bool(p.get("rebuild_3d", False)),
                },
            )
            try:
                validation = self._job_apply_route_pcb(sub)
            except Exception as e:
                validation = {"error": str(e)}
                self.log(job, f"Auto-apply failed: {e}")

        self.set_progress(job, 100, "complete")
        return {
            "total_length_mm": route.total_length_mm,
            "via_count": route.via_count,
            "clearance_violations": route.clearance_violations,
            "segments": len(route.segments),
            "unrouted": route.unrouted_nets,
            "notes": route.notes,
            "quality": q,
            "net_reports": d.get("net_reports", [])[:50],
            "length_by_layer_mm": d.get("length_by_layer_mm"),
            "artifact": str(outp.relative_to(ROOT)),
            "route": d,
            "validation": validation,
        }

    def _job_improve(self, job: Job) -> dict[str, Any]:
        """Continuous place+route improvement with live score until goal or timeout."""
        from physics_router.continuous_improve import ImproveConfig, continuous_improve

        p = job.params
        timeout_s = float(p.get("timeout_s", p.get("timeout", 120)))
        target_grade = (
            str(p.get("target_grade", p.get("grade", "A"))).upper()[:1] or "A"
        )
        min_score = float(p.get("min_score", 0) or 0)
        if min_score <= 0:
            from physics_router.continuous_improve import min_score_for_grade

            min_score = min_score_for_grade(target_grade)
        require_drc = bool(p.get("require_drc_clean", True))
        require_complete = bool(p.get("require_complete", True))
        do_place = bool(p.get("do_place", p.get("place", True)))
        do_route = bool(p.get("do_route", p.get("route", True)))
        clearance = float(p.get("clearance_mm", 0.2))
        grid = float(p.get("grid_mm", 0.25))
        max_rounds = p.get("max_rounds")
        if max_rounds is not None:
            max_rounds = int(max_rounds)

        with self.lock:
            board = copy.deepcopy(self.board())
            cfg = copy.deepcopy(self.config)
            pcb_for_drc = self.pcb_path
        require_kicad = bool(p.get("require_kicad_drc", True))
        icfg = ImproveConfig(
            timeout_s=timeout_s,
            min_score=min_score,
            target_grade=target_grade,
            require_drc_clean=require_drc,
            require_complete=require_complete,
            do_place=do_place,
            do_route=do_route,
            clearance_mm=clearance,
            grid_mm=grid,
            max_rounds=max_rounds,
            place_candidates=int(p.get("place_candidates", 2)),
            place_sa_iterations=int(p.get("place_sa_iterations", 200)),
            prefer_native=bool(p.get("prefer_native", True)),
            # TopoR multi-variant rounds are slow; default off for responsive improve
            allow_topor_rounds=bool(p.get("allow_topor_rounds", False)),
            pcb_path=str(pcb_for_drc) if pcb_for_drc else None,
            require_kicad_drc=require_kicad and bool(pcb_for_drc),
            kicad_drc_every_round=bool(p.get("kicad_drc_every_round", False)),
            kicad_drc_timeout_s=float(p.get("kicad_drc_timeout_s", 120)),
        )
        self.set_progress(job, 1, "continuous improve")
        self.log(
            job,
            f"Improve: timeout={timeout_s:.0f}s target={target_grade} "
            f"min_score≥{min_score:.0f} drc_clean={require_drc} "
            f"kicad_drc={icfg.require_kicad_drc} place={do_place} route={do_route}",
        )

        last_pub = {"t": 0.0}
        t_job0 = time.time()

        def on_progress(ev: dict[str, Any]) -> None:
            event = ev.get("event")
            # Wall-clock progress so the bar always moves (events may omit elapsed_s)
            elapsed = float(ev.get("elapsed_s") or 0) or (time.time() - t_job0)
            frac = 5.0 + 90.0 * min(1.0, elapsed / max(timeout_s, 1.0))
            best = ev.get("best") or {}
            if event == "stage":
                self.set_progress(
                    job,
                    frac,
                    f"r{ev.get('round')} {ev.get('stage')} · {ev.get('strategy')}",
                )
            elif event == "snapshot":
                self.set_progress(
                    job,
                    frac,
                    f"r{ev.get('round')} {ev.get('grade')}/{ev.get('score')} "
                    f"viol={ev.get('violations')} best="
                    f"{(best or {}).get('grade', '—')}/{(best or {}).get('score', '—')}",
                )
                kicad_bit = ""
                if ev.get("kicad_available"):
                    kicad_bit = (
                        f" kicad={'PASS' if ev.get('kicad_passed') else 'FAIL'}"
                        f"/{ev.get('kicad_copper_violations')}"
                    )
                self.log(
                    job,
                    f"  r{ev.get('round')} [{ev.get('strategy')}] "
                    f"grade={ev.get('grade')} score={ev.get('score')} "
                    f"viol={ev.get('violations')} (S={ev.get('shorts')} "
                    f"sp={ev.get('spacing')} out={ev.get('outline')}) "
                    f"vias={ev.get('vias')} unrouted={ev.get('unrouted')}"
                    f"{kicad_bit}" + (" ★BEST" if ev.get("is_best") else ""),
                )
                for s in (ev.get("kicad_samples") or [])[:4]:
                    self.log(job, f"    kicad: {s}")
                # Live copper + score on session (throttle ~2/s)
                now = time.time()
                if now - last_pub["t"] >= 0.35:
                    last_pub["t"] = now
                    partial = ev.get("partial")
                    if (
                        isinstance(partial, dict)
                        and partial.get("segments") is not None
                    ):
                        try:
                            self._publish_live_route(partial, "topor")
                        except Exception as e:
                            self.log(job, f"  live preview skip: {e}")
                    with self.lock:
                        self.last_score = {
                            "score": {
                                "route_score": ev.get("score"),
                                "grade": ev.get("grade"),
                                "violations": ev.get("violations"),
                                "vias": ev.get("vias"),
                                "unrouted": ev.get("unrouted"),
                            },
                            "score_total": ev.get("score"),
                            "notes": [
                                f"live improve r{ev.get('round')}: "
                                f"{ev.get('grade')}/{ev.get('score')} "
                                f"viol={ev.get('violations')}"
                            ],
                            "improve_live": ev,
                        }
            elif event == "route_progress":
                done_n = int(ev.get("done") or 0)
                total = max(1, int(ev.get("total") or 1))
                # Blend time with net progress so native (no per-net) still moves
                net_frac = 5.0 + 90.0 * (done_n / total)
                self.set_progress(
                    job,
                    max(frac, min(95.0, net_frac * 0.35 + frac * 0.65)),
                    f"r? {ev.get('strategy')} {ev.get('stage')} "
                    f"{done_n}/{total} · {ev.get('net')}",
                )
                partial = ev.get("partial")
                if isinstance(partial, dict) and partial.get("segments") is not None:
                    now = time.time()
                    if now - last_pub["t"] >= 0.5:
                        last_pub["t"] = now
                        try:
                            self._publish_live_route(partial, "topor")
                        except Exception:
                            pass
            elif event == "done":
                self.set_progress(
                    job,
                    98,
                    f"done · {ev.get('stop_reason')} · "
                    f"best={(best or {}).get('grade')}/{(best or {}).get('score')}",
                )

        def cancel_cb() -> bool:
            return job.status == "cancelled"

        result = continuous_improve(
            board,
            cfg,
            improve=icfg,
            progress_cb=on_progress,
            cancel_cb=cancel_cb,
        )

        route = result.route
        out_payload = result.to_dict()
        if route is not None:
            d = route.to_dict()
            q = route.quality or route.compute_quality()
            with self.lock:
                if result.placement_positions:
                    apply_positions(self.board(), result.placement_positions)
                self.routes = {"topor": route}
                self.selected_route = "topor"
                self.last_score = {
                    "score": {
                        "route_score": (
                            result.best_snapshot.score if result.best_snapshot else None
                        ),
                        "grade": (
                            result.best_snapshot.grade if result.best_snapshot else None
                        ),
                        "violations": (
                            result.best_snapshot.violations
                            if result.best_snapshot
                            else None
                        ),
                    },
                    "score_total": (
                        result.best_snapshot.score if result.best_snapshot else None
                    ),
                    "notes": list(result.notes[-8:]),
                    "improve": out_payload,
                }
                self._refresh_viewer()
                outp = WORK_DIR / f"improve_{job.id}.json"
                outp.write_text(
                    json.dumps(
                        {
                            **out_payload,
                            "route": d,
                            "quality": q,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            self.log(
                job,
                f"Improve finished: {result.stop_reason} met_goal={result.met_goal} "
                f"best={(result.best_snapshot.summary if result.best_snapshot else '—')}",
            )
            for n in result.notes[-12:]:
                self.log(job, f"  {n}")
            out_payload.update(
                {
                    "total_length_mm": route.total_length_mm,
                    "via_count": route.via_count,
                    "clearance_violations": route.clearance_violations,
                    "segments": len(route.segments),
                    "unrouted": route.unrouted_nets,
                    "quality": q,
                    "artifact": str(outp.relative_to(ROOT)),
                    "route": d,
                }
            )
        else:
            with self.lock:
                if result.placement_positions:
                    apply_positions(self.board(), result.placement_positions)
                    self._refresh_viewer()
            self.log(job, f"Improve finished without route: {result.stop_reason}")
        self.set_progress(job, 100, "complete")
        return out_payload

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
        self.log(
            job, f"Pre-route: density={out.get('estimated_density_pins_per_cm2', '—')}"
        )
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
        notes = [
            n
            for n in sb.notes
            if "spice" in n.lower()
            or "ngspice" in n.lower()
            or "L≈" in n
            or "induct" in n.lower()
        ]
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
        paths = export_openems_bundle(
            out_dir, board=board, routes=route_obj, config=cfg
        )
        self.set_progress(job, 70, "openems score proxy")
        sb = geometric_score(board, cfg)
        sb = apply_simulation_scores(
            board, cfg, sb, spice=None, openems=OpenEMSBackend()
        )
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
                    self.viewer_payload.setdefault("assets", {})["emi_geometry"] = (
                        "emi_geometry.json"
                    )
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
        return {
            "dsn": str(Path(path).relative_to(ROOT)),
            "readme": str((out.parent / "FREEROUTING.md").relative_to(ROOT)),
        }

    def _job_export_step(self, job: Job) -> dict[str, Any]:
        """Full STEP with component models + mask/silk/copper (not board-only)."""
        self.set_progress(job, 10, "export STEP")
        with self.lock:
            pcb = self.pcb_path
            preset = self.preset
        if not pcb or not Path(pcb).exists():
            self.log(job, "No KiCad PCB in session — STEP needs a real .kicad_pcb")
            return {
                "skipped": True,
                "reason": "no pcb path (use halo-90 preset or set pcb)",
            }
        from physics_router.kicad_tools import export_step

        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        out = ASSETS_DIR / f"{preset or Path(pcb).stem}_full.step"
        self.set_progress(job, 30, "kicad-cli step (components + mask + silk)")
        self.log(job, f"Exporting STEP with footprint models → {out}")
        path = export_step(
            Path(pcb),
            out,
            board_only=False,
            no_components=False,
            include_tracks=True,
            include_pads=True,
            include_zones=True,
            include_inner_copper=True,
            include_silkscreen=True,
            include_soldermask=True,
            subst_models=True,
        )
        with self.lock:
            self.step_url = f"/assets/{path.name}"
            self._refresh_viewer()
        self.set_progress(job, 100, "done")
        self.log(job, f"STEP {path.stat().st_size // 1024} KB")
        return {
            "step": str(path.relative_to(ROOT)),
            "url": f"/assets/{path.name}",
            "bytes": path.stat().st_size,
            "includes": [
                "components STEP",
                "tracks",
                "pads",
                "soldermask",
                "silkscreen",
                "inner copper",
            ],
        }

    def _job_apply_route_pcb(self, job: Job) -> dict[str, Any]:
        """Write selected route copper into a KiCad PCB (for 3D / DRC / fab)."""
        from physics_router.router import RouteResult, append_routes_to_kicad_pcb

        variant = job.params.get("variant") or self.selected_route or "topor"
        rebuild_3d = bool(job.params.get("rebuild_3d", False))
        self.set_progress(job, 10, f"apply {variant}")
        with self.lock:
            route = self.routes.get(variant)
            # Prefer original PCB as copper base, not a previously routed copy
            base_pcb = self.pcb_path
            preset = self.preset or "board"
        if route is None:
            raise RuntimeError(
                f"No route variant '{variant}'. Run Guide or Clearance route first."
            )
        if not base_pcb or not Path(base_pcb).exists():
            raise RuntimeError(
                "No .kicad_pcb in session — load HALO-90 or set pcb path"
            )

        if not isinstance(route, RouteResult):
            # rebuild from dict if needed
            from physics_router.router import CopperArea, RouteSegment, Via

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
                for s in (route.get("segments") or [])
            ]
            vias = [
                Via(
                    x=v["x"],
                    y=v["y"],
                    net=v.get("net", ""),
                    size_mm=v.get("size_mm", 0.8),
                    drill_mm=v.get("drill_mm", 0.4),
                )
                for v in (route.get("vias") or [])
            ]
            areas = [
                CopperArea(
                    outline=[tuple(point) for point in area.get("outline") or []],
                    layer=area.get("layer", "F.Cu"),
                    net=area.get("net", ""),
                    clearance_mm=area.get("clearance_mm", 0.2),
                    min_thickness_mm=area.get("min_thickness_mm", 0.25),
                    priority=area.get("priority", 0),
                )
                for area in (route.get("areas") or [])
            ]
            route = RouteResult(
                segments=segs,
                vias=vias,
                areas=areas,
                via_count=int(route.get("via_count") or len(vias)),
                total_length_mm=float(route.get("total_length_mm") or 0),
                unrouted_nets=list(route.get("unrouted_nets") or []),
                clearance_violations=int(route.get("clearance_violations") or 0),
                notes=list(route.get("notes") or []),
            )

        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        # Always bake onto the *source* board (not a prior routed file) when possible
        source = Path(base_pcb)
        # If current path is already a routed file, prefer stock halo/original from preset
        if "routed" in source.name and HALO_PCB.exists() and preset == "halo-90":
            source = HALO_PCB
        out_pcb = ASSETS_DIR / f"{preset}_routed_{variant}.kicad_pcb"
        self.log(job, f"Writing copper from '{variant}' → {out_pcb.name}")
        self.set_progress(job, 40, "write kicad_pcb")
        path = append_routes_to_kicad_pcb(
            str(source), str(out_pcb), route, replace_previous=True
        )
        with self.lock:
            self.selected_route = variant
            self.routed_pcb_path = str(path)
            # Point session PCB at routed board so 3D export includes copper
            self.pcb_path = str(path)
            self.board_source = "pcb"
            self._refresh_viewer()
        self.set_progress(job, 55, "pcb written")
        self.log(
            job,
            f"Applied {len(route.segments)} segments + {route.via_count} vias "
            f"({route.total_length_mm:.1f} mm)",
        )
        out: dict[str, Any] = {
            "variant": variant,
            "pcb": str(path.relative_to(ROOT)),
            "url": f"/assets/{path.name}",
            "segments": len(route.segments),
            "vias": route.via_count,
            "total_length_mm": route.total_length_mm,
            "quality": route.quality
            or (route.compute_quality() if hasattr(route, "compute_quality") else {}),
        }

        # Official KiCad DRC (+ ERC when schematic present) after every apply
        try:
            from physics_router.kicad_tools import (
                find_schematic_for_pcb,
                run_drc,
                run_erc,
            )

            self.set_progress(job, 60, "kicad DRC")
            drc_dir = WORK_DIR / f"drc_apply_{job.id}"
            drc_dir.mkdir(parents=True, exist_ok=True)
            drc = run_drc(path, drc_dir / "drc.json")
            drc_sum = drc.to_dict() if hasattr(drc, "to_dict") else {}
            out["drc"] = drc_sum
            self.log(
                job,
                f"DRC: errors={drc_sum.get('error_count')} warn={drc_sum.get('warning_count')} "
                f"copper={drc_sum.get('copper_violation_count')} unconn={drc_sum.get('unconnected_count')}",
            )
            sch = find_schematic_for_pcb(source)
            if sch is not None:
                self.set_progress(job, 68, "kicad ERC")
                erc = run_erc(sch, drc_dir / "erc.json")
                out["erc"] = erc
                self.log(
                    job,
                    f"ERC: errors={erc.get('error_count')} warn={erc.get('warning_count')} "
                    f"passed={erc.get('passed')}",
                )
            else:
                out["erc"] = {"skipped": True, "reason": "no schematic next to PCB"}
        except Exception as e:
            out["drc"] = {"error": str(e)}
            self.log(job, f"DRC/ERC skipped: {e}")

        if rebuild_3d:
            self.set_progress(job, 75, "rebuild 3D GLB from routed PCB")
            self.log(job, "Re-exporting KiCad GLB with applied copper…")
            sub = Job(
                id=f"{job.id}-glb", type="export_board_3d", params={"also_step": False}
            )
            glb_res = self._job_export_board_3d(sub)
            out["glb"] = glb_res
            self.log(job, f"GLB: {glb_res.get('url') or glb_res}")
        self.set_progress(job, 100, "done")
        return out

    def _job_export_board_3d(self, job: Job) -> dict[str, Any]:
        """KiCad GLB for three.js: component STEP models + all visual layers."""
        self.set_progress(job, 5, "export board 3D")
        with self.lock:
            # Prefer last applied routed board (includes copper)
            pcb = self.routed_pcb_path or self.pcb_path
            preset = self.preset or "board"
        if not pcb or not Path(pcb).exists():
            return {"skipped": True, "reason": "no pcb path"}
        from physics_router.kicad_tools import export_board_visual_3d, find_kicad_cli

        if find_kicad_cli() is None:
            raise FileNotFoundError(
                "kicad-cli not found — install KiCad or set KICAD_CLI"
            )
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        also_step = bool(job.params.get("also_step", False))
        stem = (
            f"{preset}_routed"
            if self.routed_pcb_path and Path(pcb).name.find("routed") >= 0
            else (preset if preset != "synthetic" else Path(pcb).stem)
        )
        self.set_progress(job, 15, "kicad-cli glb (STEP models + mask + silk + copper)")
        self.log(
            job,
            "Exporting GLB: footprint .step/.stp models, tracks, pads, zones, "
            "inner copper, soldermask, silkscreen…",
        )
        result = export_board_visual_3d(
            Path(pcb),
            ASSETS_DIR,
            stem=stem,
            also_step=also_step,
        )
        glb_path = Path(result["glb"])
        # Stable names: halo-90.glb (stock) and halo-90_routed.glb (with copper)
        import shutil

        stable = ASSETS_DIR / f"{stem}.glb"
        if glb_path.resolve() != stable.resolve() and glb_path.exists():
            shutil.copy2(glb_path, stable)
            result["glb"] = str(stable)
            glb_path = stable
        # Also publish as preset.glb when this is the active view model
        view_name = ASSETS_DIR / f"{preset}.glb"
        if glb_path.resolve() != view_name.resolve():
            shutil.copy2(glb_path, view_name)
        with self.lock:
            self.glb_url = f"/assets/{view_name.name}"
            if result.get("step"):
                self.step_url = f"/assets/{Path(result['step']).name}"
            self._refresh_viewer()
        self.set_progress(job, 100, "done")
        self.log(
            job, f"GLB ready {glb_path.stat().st_size // 1024} KB → {self.glb_url}"
        )
        return {
            **{
                k: (
                    str(Path(v).relative_to(ROOT))
                    if k in ("glb", "step", "pcb") and v
                    else v
                )
                for k, v in result.items()
            },
            "url": self.glb_url,
            "viewer": "three.js GLTFLoader",
        }

    def _job_drc(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 10, "DRC")
        with self.lock:
            pcb = self.routed_pcb_path or self.pcb_path
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
        summary = (
            report.to_dict() if hasattr(report, "to_dict") else {"result": str(report)}
        )
        self.log(job, f"DRC summary: {summary}")
        return summary

    def _job_erc(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 10, "ERC")
        with self.lock:
            pcb = self.pcb_path
        from physics_router.kicad_tools import find_schematic_for_pcb, run_erc

        sch = None
        if job.params.get("sch") and Path(job.params["sch"]).exists():
            sch = Path(job.params["sch"])
        elif pcb:
            sch = find_schematic_for_pcb(pcb)
        if sch is None or not Path(sch).exists():
            return {"skipped": True, "reason": "no schematic found"}
        out_dir = WORK_DIR / f"erc_{job.id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        self.set_progress(job, 40, "kicad-cli sch erc")
        summary = run_erc(sch, out_dir / "erc.json")
        self.set_progress(job, 100, "done")
        self.log(job, f"ERC: {summary}")
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
        proc = subprocess.run(
            args, cwd=str(ROOT), capture_output=True, text=True, env=env, timeout=600
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.splitlines()[-50:]:
            self.log(job, line)
        self.set_progress(job, 100, "done")
        return {
            "returncode": proc.returncode,
            "output": out[-6000:],
            "ok": proc.returncode == 0,
        }

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
        return {
            "viewer_data": "viewer/viewer_data.json",
            "dashboard": "viewer/dashboard.html",
        }

    def _job_compare(self, job: Job) -> dict[str, Any]:
        self.set_progress(job, 20, "compare")
        with self.lock:
            topor = self.routes.get("topor") or self.routes.get("guide")
        if topor is None:
            raise RuntimeError(
                "No route in session — run route_guide or route_clearance first"
            )
        from physics_router.router import RouteResult

        if isinstance(topor, RouteResult):
            td = topor.to_dict()
        else:
            td = topor
        # Write temp metrics
        tmp = WORK_DIR / f"cmp_topor_{job.id}.json"
        tmp.write_text(json.dumps(td, indent=2) + "\n", encoding="utf-8")
        baseline = None
        if (
            job.params.get("baseline_json")
            and Path(job.params["baseline_json"]).exists()
        ):
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
            "route_topor",
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
            sub = Job(
                id=f"{job.id}-{step}",
                type=step,
                params=job.params.get(step + "_params") or {},
            )
            # Run sub-job inline sharing progress band
            try:
                # Temporarily nest by calling runner with progress remap
                child_result = self._run_job(sub)
                results[step] = {
                    "ok": True,
                    "result": child_result,
                    "log_tail": sub.log[-10:],
                }
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
            jobs = [
                self.jobs[i].to_dict(full=False) for i in reversed(self.job_order[-50:])
            ]
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
                    "glb": self.glb_url,
                    "step": self.step_url,
                    "selected_route": self.selected_route,
                    "routed_pcb": self.routed_pcb_path,
                },
                "config": self.config.model_dump(mode="json"),
                "config_text": self.config_text,
                "selected_route": self.selected_route,
                "routed_pcb": (
                    str(Path(self.routed_pcb_path).relative_to(ROOT))
                    if self.routed_pcb_path and Path(self.routed_pcb_path).exists()
                    else None
                ),
                "routes": {
                    k: (v.to_dict() if hasattr(v, "to_dict") else v)
                    for k, v in self.routes.items()
                },
                "last_score": self.last_score,
                "last_placement": {
                    "best_candidate_id": (self.last_placement or {}).get(
                        "best_candidate_id"
                    ),
                    "best_score": (self.last_placement or {}).get("best_score"),
                }
                if self.last_placement
                else None,
                "last_comparison": self.last_comparison,
                "jobs": jobs,
                "job_types": JOB_CATALOG,
                "presets": list_presets(),
                "fab_profile": self.fab_profile,
                "fab_profiles": _fab_profiles_payload(),
            }


def _fab_profiles_payload() -> list[dict[str, Any]]:
    from physics_router.design_rules import list_jlcpcb_profiles

    return list_jlcpcb_profiles()


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
        "id": "route_topor",
        "label": "TopoR free-angle route",
        "group": "route",
        "description": "Isotropic free-angle TopoR pipeline: multi-variant search, rubberband + via minimize, live 2D preview",
    },
    {
        "id": "improve",
        "label": "Continuous improve",
        "group": "route",
        "description": "Loop place+route with live score until timeout or target grade + full DRC pass",
    },
    {
        "id": "apply_route_pcb",
        "label": "Apply route → KiCad PCB",
        "group": "route",
        "description": "Write TopoR copper into .kicad_pcb; optional GLB rebuild for 3D",
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
        "id": "export_board_3d",
        "label": "Export board 3D (GLB)",
        "group": "export",
        "description": "KiCad GLB: component STEP models + copper + soldermask + silkscreen for three.js",
    },
    {
        "id": "export_step",
        "label": "Export STEP (KiCad)",
        "group": "export",
        "description": "Full STEP with component models + mask/silk/copper (needs PCB)",
    },
    {
        "id": "drc",
        "label": "KiCad DRC",
        "group": "validate",
        "description": "Official kicad-cli pcb drc oracle (needs PCB)",
    },
    {
        "id": "erc",
        "label": "KiCad ERC",
        "group": "validate",
        "description": "Official kicad-cli sch erc (needs schematic)",
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

    def guess_type(self, path: str) -> str:  # type: ignore[override]
        low = path.lower().split("?", 1)[0]
        if low.endswith(".glb"):
            return "model/gltf-binary"
        if low.endswith(".gltf"):
            return "model/gltf+json"
        if low.endswith((".step", ".stp")):
            return "application/step"
        return super().guess_type(path)

    def end_headers(self) -> None:
        # Allow canvas/workers to fetch GLB from same origin without cache traps
        path = (self.path or "").split("?", 1)[0]
        if path.endswith((".glb", ".gltf")):
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

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
                try:
                    from physics_router.native_bridge import info as native_info

                    native = native_info()
                except Exception as exc:  # noqa: BLE001
                    native = {"available": False, "error": str(exc)}
                return self._json(
                    200,
                    {"ok": True, "version": __version__, "native": native},
                )
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
                    jobs = [
                        STATE.jobs[i].to_dict(full=False)
                        for i in reversed(STATE.job_order[-50:])
                    ]
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
                    for p in sorted(
                        WORK_DIR.rglob("*"),
                        key=lambda x: x.stat().st_mtime,
                        reverse=True,
                    )[:80]:
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
            return self._json(
                500, {"error": str(e), "trace": traceback.format_exc()[-1500:]}
            )

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
            if path == "/api/routes/select":
                name = body.get("variant") or body.get("name")
                with STATE.lock:
                    if name and name not in STATE.routes:
                        return self._json(
                            404,
                            {
                                "error": f"unknown variant {name}",
                                "known": list(STATE.routes),
                            },
                        )
                    STATE.selected_route = name
                    snap = STATE.snapshot()
                return self._json(
                    200, {"selected_route": name, "session": snap.get("session")}
                )
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

                    text = yaml.safe_dump(
                        cfg.model_dump(mode="json"),
                        sort_keys=False,
                        default_flow_style=False,
                    )
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
            if path == "/api/fab-profile":
                from physics_router.design_rules import (
                    JLCPCB_PROFILES,
                    parse_jlc_profile,
                )

                pid = str(body.get("profile") or body.get("id") or "").strip()
                if not pid:
                    return self._json(400, {"error": "profile required"})
                # Normalize aliases
                layers, agg = parse_jlc_profile(pid)
                norm = f"{layers}layer_{'capability' if agg else 'recommended'}"
                if norm not in JLCPCB_PROFILES and pid not in JLCPCB_PROFILES:
                    return self._json(
                        400,
                        {
                            "error": f"unknown profile {pid}",
                            "known": list(JLCPCB_PROFILES.keys()),
                        },
                    )
                with STATE.lock:
                    STATE.fab_profile = pid if pid in JLCPCB_PROFILES else norm
                    snap = STATE.snapshot()
                return self._json(
                    200,
                    {
                        "fab_profile": STATE.fab_profile,
                        "session": snap.get("session"),
                        "fab_profiles": snap.get("fab_profiles"),
                    },
                )
            return self._json(404, {"error": f"unknown api {path}"})
        except Exception as e:
            return self._json(
                500, {"error": str(e), "trace": traceback.format_exc()[-1500:]}
            )

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
