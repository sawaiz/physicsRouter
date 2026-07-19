"""Continuous place + route improvement with live score until goal or timeout.

Keeps trying alternate placement seeds and routing strategies, publishing a
live quality snapshot after every attempt. Stops when:

* elapsed time ≥ ``timeout_s``, or
* cancel callback returns True, or
* goal met: score ≥ ``min_score`` (grade high enough) **and** full DRC pass
  (0 shorts / spacing / outline) when ``require_drc_clean`` is set.

Best-so-far is retained so a timeout still returns the strongest result.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from physics_router.models import BoardModel, PlacementConfig
from physics_router.placement import apply_positions, optimize_placement
from physics_router.router import (
    RouteResult,
    attach_router_drc,
    clearance_aware_route,
    native_drc_check,
)
from physics_router.routing_strategies import _net_order_variants, topor_style_route

ProgressCb = Callable[[dict[str, Any]], None]
CancelCb = Callable[[], bool]

_GRADE_RANK = {"A": 5, "B": 4, "C": 3, "D": 2, "F": 1}


def grade_rank(grade: str | None) -> int:
    return _GRADE_RANK.get((grade or "F").upper()[:1], 0)


def min_score_for_grade(grade: str) -> float:
    g = (grade or "A").upper()[:1]
    return {"A": 90.0, "B": 75.0, "C": 55.0, "D": 35.0}.get(g, 90.0)


@dataclass
class ImproveConfig:
    """Stop criteria and loop knobs for continuous improve."""

    timeout_s: float = 120.0
    min_score: float = 90.0
    target_grade: str = "A"
    require_drc_clean: bool = True
    require_complete: bool = True  # no fully unrouted nets
    do_place: bool = True
    do_route: bool = True
    clearance_mm: float = 0.2
    grid_mm: float = 0.25
    max_rounds: int | None = None
    # Placement budget per reseed (kept small for interactive loops)
    place_candidates: int = 2
    place_sa_iterations: int = 200
    prefer_native: bool = True
    # When True, use full TopoR multi-variant on some rounds (slower, higher quality)
    allow_topor_rounds: bool = True
    # Official KiCad DRC via kicad-cli (authoritative; native check is a fast filter)
    pcb_path: str | None = None
    require_kicad_drc: bool = True
    # Run kicad-cli every round if True; else only when native looks clean / final
    kicad_drc_every_round: bool = False
    kicad_drc_timeout_s: float = 120.0


@dataclass
class ImproveSnapshot:
    round: int
    elapsed_s: float
    strategy: str
    score: float
    grade: str
    violations: int
    shorts: int
    spacing: int
    outline: int
    vias: int
    unrouted: int
    length_mm: float
    placement_cost: float | None
    stage: str
    is_best: bool = False
    met_goal: bool = False
    summary: str = ""
    kicad_available: bool = False
    kicad_copper_violations: int = 0
    kicad_errors: int = 0
    kicad_passed: bool | None = None
    kicad_samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "elapsed_s": round(self.elapsed_s, 2),
            "strategy": self.strategy,
            "score": self.score,
            "grade": self.grade,
            "violations": self.violations,
            "shorts": self.shorts,
            "spacing": self.spacing,
            "outline": self.outline,
            "vias": self.vias,
            "unrouted": self.unrouted,
            "length_mm": round(self.length_mm, 2),
            "placement_cost": self.placement_cost,
            "stage": self.stage,
            "is_best": self.is_best,
            "met_goal": self.met_goal,
            "summary": self.summary,
            "kicad_available": self.kicad_available,
            "kicad_copper_violations": self.kicad_copper_violations,
            "kicad_errors": self.kicad_errors,
            "kicad_passed": self.kicad_passed,
            "kicad_samples": list(self.kicad_samples)[:8],
        }


@dataclass
class ImproveResult:
    board: BoardModel
    route: RouteResult | None
    best_snapshot: ImproveSnapshot | None
    history: list[ImproveSnapshot] = field(default_factory=list)
    stop_reason: str = "unknown"
    met_goal: bool = False
    placement_positions: dict[str, tuple[float, float, float]] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_reason": self.stop_reason,
            "met_goal": self.met_goal,
            "best": self.best_snapshot.to_dict() if self.best_snapshot else None,
            "history": [h.to_dict() for h in self.history],
            "notes": self.notes,
            "route_quality": (self.route.quality if self.route else None),
            "route_summary": (
                (self.route.quality or {}).get("summary") if self.route else None
            ),
            "has_route": bool(self.route and self.route.segments),
            "placement_positions": (
                {
                    ref: {"x_mm": p[0], "y_mm": p[1], "rotation_deg": p[2]}
                    for ref, p in (self.placement_positions or {}).items()
                }
                if self.placement_positions
                else None
            ),
        }


def _route_score_key(snap: ImproveSnapshot) -> tuple:
    """Higher is better. Prefer KiCad-clean, then native-clean, score, completion."""
    # Unknown kicad ranks below known-clean, above known-fail
    if snap.kicad_available:
        kicad_rank = 2 if snap.kicad_passed else 0
    else:
        kicad_rank = 1
    clean = 1 if snap.violations == 0 else 0
    complete = 1 if snap.unrouted == 0 else 0
    return (
        kicad_rank,
        clean,
        complete,
        snap.score,
        -snap.kicad_copper_violations,
        -snap.unrouted,
        -snap.vias,
        -snap.length_mm,
    )


def _apply_kicad_to_snapshot(
    snap: ImproveSnapshot,
    route: RouteResult,
    cfg: ImproveConfig,
    *,
    force: bool = False,
) -> ImproveSnapshot:
    """Optionally run official KiCad DRC and fold into score/violations."""
    if not cfg.pcb_path or not cfg.require_kicad_drc:
        return snap
    # Skip expensive kicad-cli when native is messy unless forced / every_round
    if (
        not force
        and not cfg.kicad_drc_every_round
        and snap.violations > 8
        and snap.score < 50
    ):
        return snap
    try:
        from physics_router.kicad_tools import kicad_drc_route

        kd = kicad_drc_route(
            cfg.pcb_path,
            route,
            timeout_s=float(cfg.kicad_drc_timeout_s),
            keep_files=False,
        )
    except Exception as exc:
        snap.summary = f"{snap.summary} · kicad_drc error: {exc}"
        return snap

    snap.kicad_available = bool(kd.get("available"))
    if not snap.kicad_available:
        snap.summary = f"{snap.summary} · kicad: {kd.get('error', 'unavailable')}"
        return snap

    copper = int(kd.get("copper_violation_count") or 0)
    copper_err = int(kd.get("copper_error_count") or kd.get("error_count") or 0)
    snap.kicad_copper_violations = copper
    snap.kicad_errors = copper_err
    snap.kicad_passed = bool(kd.get("copper_passed"))
    snap.kicad_samples = list(kd.get("samples") or [])[:8]

    # Authoritative: the serialized route must carry the same violation count as
    # the snapshot.  Previously this was only applied when KiCad exceeded the
    # native count and was later erased by the final native DRC pass.
    combined = max(snap.violations, copper, copper_err)
    route.clearance_violations = combined
    q = route.compute_quality()
    snap.score = float(q.get("score") or snap.score)
    snap.grade = str(q.get("grade") or snap.grade)
    snap.violations = combined
    snap.summary = str(q.get("summary") or snap.summary)
    # Always annotate summary with KiCad oracle
    snap.summary = (
        f"{snap.summary} · KiCad DRC copper={copper} errors={copper_err} "
        f"({'PASS' if snap.kicad_passed else 'FAIL'})"
    )
    route.quality = {
        **(route.quality or {}),
        "kicad_drc": {
            "copper_violation_count": copper,
            "error_count": copper_err,
            "passed": snap.kicad_passed,
            "samples": snap.kicad_samples,
            "by_type": kd.get("by_type") or {},
            "kicad_version": kd.get("kicad_version") or "",
        },
    }
    return snap


def _snapshot_from_route(
    route: RouteResult,
    board: BoardModel,
    *,
    round_i: int,
    elapsed: float,
    strategy: str,
    stage: str,
    placement_cost: float | None,
    clearance_mm: float,
    cfg: ImproveConfig | None = None,
    run_kicad: bool = False,
) -> ImproveSnapshot:
    attach_router_drc(route, clearance_mm=clearance_mm, board=board)
    q = route.compute_quality()
    drc = (q.get("drc") or {}) if isinstance(q, dict) else {}
    # Prefer attached DRC fields; fall back to native check
    if not drc:
        raw = native_drc_check(route, clearance_mm=clearance_mm, board=board)
        drc = {
            "violations": raw["violations"],
            "shorts": raw["shorts"],
            "spacing": raw["spacing"],
            "outline_outside": raw.get("outline_outside", 0),
        }
    viol = int(drc.get("violations") or route.clearance_violations or 0)
    snap = ImproveSnapshot(
        round=round_i,
        elapsed_s=elapsed,
        strategy=strategy,
        score=float(q.get("score") or 0.0),
        grade=str(q.get("grade") or "F"),
        violations=viol,
        shorts=int(drc.get("shorts") or 0),
        spacing=int(drc.get("spacing") or 0),
        outline=int(drc.get("outline_outside") or drc.get("outline") or 0),
        vias=int(route.via_count),
        unrouted=len(route.unrouted_nets or []),
        length_mm=float(route.total_length_mm),
        placement_cost=placement_cost,
        stage=stage,
        summary=str(q.get("summary") or ""),
    )
    if cfg is not None and run_kicad:
        snap = _apply_kicad_to_snapshot(snap, route, cfg, force=True)
    return snap


def goal_met(snap: ImproveSnapshot, cfg: ImproveConfig) -> bool:
    if cfg.require_drc_clean and snap.violations > 0:
        return False
    if cfg.require_complete and snap.unrouted > 0:
        return False
    # Official KiCad DRC is required for goal when enabled and available
    if cfg.require_kicad_drc and cfg.pcb_path:
        if snap.kicad_available and not snap.kicad_passed:
            return False
        # If we never ran KiCad this round, do not claim goal
        if not snap.kicad_available and cfg.require_kicad_drc:
            return False
    # Score threshold (primary); grade is informational / alternate
    min_s = max(float(cfg.min_score), min_score_for_grade(cfg.target_grade))
    if snap.score >= min_s:
        return True
    if grade_rank(snap.grade) >= grade_rank(cfg.target_grade) and snap.score >= min_s - 0.05:
        return True
    return False


def _strategy_plan(cfg: ImproveConfig, board: BoardModel) -> list[dict[str, Any]]:
    """Ordered diversifying strategies cycled each round."""
    orders = _net_order_variants(board, None)
    plan: list[dict[str, Any]] = []
    # Hybrid multi-strategy free-angle first (matrix/power/critical/general)
    plan.append({"name": "hybrid", "kind": "hybrid", "grid_mm": cfg.grid_mm})
    if cfg.prefer_native:
        plan.append(
            {
                "name": "native",
                "kind": "clearance",
                "prefer_native": True,
                "grid_mm": cfg.grid_mm,
            }
        )
        plan.append(
            {
                "name": "native_fine",
                "kind": "clearance",
                "prefer_native": True,
                "grid_mm": min(float(cfg.grid_mm), 0.15),
            }
        )
    for name, order in orders[:3]:
        plan.append(
            {
                "name": f"py_{name}",
                "kind": "clearance",
                "prefer_native": False,
                "grid_mm": cfg.grid_mm,
                "net_order": order,
            }
        )
    if cfg.allow_topor_rounds:
        plan.append(
            {
                "name": "topor_v1",
                "kind": "topor",
                "num_variants": 1,
                "grid_mm": cfg.grid_mm,
            }
        )
        plan.append(
            {
                "name": "topor_v2",
                "kind": "topor",
                "num_variants": 2,
                "grid_mm": min(float(cfg.grid_mm), 0.2),
            }
        )
    if not plan:
        plan.append(
            {
                "name": "clearance",
                "kind": "clearance",
                "prefer_native": False,
                "grid_mm": cfg.grid_mm,
            }
        )
    return plan


def _run_route(
    board: BoardModel,
    config: PlacementConfig | None,
    strat: dict[str, Any],
    cfg: ImproveConfig,
    progress_cb: ProgressCb | None,
) -> RouteResult:
    grid = float(strat.get("grid_mm") or cfg.grid_mm)
    cl = float(cfg.clearance_mm)
    prefer_native = bool(strat.get("prefer_native", cfg.prefer_native))

    def _net_prog(done_n: int, total: int, name: str, stage: str, detail: dict) -> None:
        if not progress_cb:
            return
        partial = (detail or {}).get("partial") if isinstance(detail, dict) else None
        progress_cb(
            {
                "event": "route_progress",
                "strategy": strat.get("name"),
                "done": done_n,
                "total": total,
                "net": name,
                "stage": stage,
                "partial": partial,
            }
        )

    if strat.get("kind") == "topor":
        return topor_style_route(
            board,
            config,
            None,
            clearance_mm=cl,
            grid_mm=grid,
            num_variants=int(strat.get("num_variants") or 1),
            progress_cb=_net_prog if progress_cb else None,
            use_planner=True,
            use_cbs=True,
            use_elastic=True,
            use_regeometry=True,
        )

    if strat.get("kind") == "hybrid":
        from physics_router.hybrid_route import hybrid_route

        # No per-net progress into clearance_aware so native path stays fast
        return hybrid_route(
            board,
            config,
            clearance_mm=cl,
            progress_cb=None,
        )

    # Native C++ path is skipped when progress_cb is set (router early-return
    # requires no live callback). Prefer the fast native route; publish after.
    use_live = bool(progress_cb) and not prefer_native
    stop_hb = threading.Event()
    if progress_cb and prefer_native:
        n_nets = max(1, len(board.nets))
        t0 = time.time()

        def _heartbeat() -> None:
            tick = 0
            while not stop_hb.wait(0.8):
                tick += 1
                # Fake net progress so UI bar advances during native (no per-net CB)
                done = min(n_nets - 1, tick)
                try:
                    progress_cb(
                        {
                            "event": "route_progress",
                            "strategy": strat.get("name"),
                            "done": done,
                            "total": n_nets,
                            "net": "(native)",
                            "stage": "native_core",
                            "elapsed_s": time.time() - t0,
                            "partial": None,
                        }
                    )
                except Exception:
                    pass

        progress_cb(
            {
                "event": "route_progress",
                "strategy": strat.get("name"),
                "done": 0,
                "total": n_nets,
                "net": "(native)",
                "stage": "native_core",
                "elapsed_s": 0.0,
                "partial": None,
            }
        )
        threading.Thread(target=_heartbeat, daemon=True).start()
    try:
        return clearance_aware_route(
            board,
            config,
            clearance_mm=cl,
            grid_mm=grid,
            soft_fallback=False,
            prefer_native=prefer_native,
            allow_vias=True,
            net_order=strat.get("net_order"),
            progress_cb=_net_prog if use_live else None,
        )
    finally:
        stop_hb.set()


def continuous_improve(
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    improve: ImproveConfig | None = None,
    progress_cb: ProgressCb | None = None,
    cancel_cb: CancelCb | None = None,
) -> ImproveResult:
    """Iterate place+route until goal, timeout, or cancel.

    ``progress_cb`` receives dicts with at least ``event`` and live score fields
    (``score``, ``grade``, ``violations``, ``best``, ``history_tail``, optional
    ``partial`` route geometry).
    """
    cfg = improve or ImproveConfig()
    # Align min_score with target grade floor
    cfg = copy.copy(cfg)
    cfg.min_score = max(float(cfg.min_score), min_score_for_grade(cfg.target_grade))

    work = copy.deepcopy(board)
    t0 = time.time()
    history: list[ImproveSnapshot] = []
    best_route: RouteResult | None = None
    best_snap: ImproveSnapshot | None = None
    best_board = copy.deepcopy(work)
    best_positions: dict[str, tuple[float, float, float]] | None = None
    placement_cost: float | None = None
    notes: list[str] = []
    stop_reason = "unknown"
    met = False

    strategies = _strategy_plan(cfg, work)
    movable = work.movable_refs() if cfg.do_place else []
    notes.append(
        f"improve: timeout={cfg.timeout_s:.0f}s target={cfg.target_grade}"
        f" min_score={cfg.min_score:.0f} drc_clean={cfg.require_drc_clean}"
        f" place={bool(movable)} strategies={len(strategies)}"
    )

    def _emit(payload: dict[str, Any]) -> None:
        if progress_cb:
            try:
                progress_cb(payload)
            except Exception:
                pass

    def _cancelled() -> bool:
        return bool(cancel_cb and cancel_cb())

    def _time_left() -> float:
        return float(cfg.timeout_s) - (time.time() - t0)

    round_i = 0
    max_rounds = cfg.max_rounds if cfg.max_rounds is not None else 10_000

    while round_i < max_rounds:
        if _cancelled():
            stop_reason = "cancelled"
            notes.append("stopped: cancelled")
            break
        if _time_left() <= 0:
            stop_reason = "timeout"
            notes.append(f"stopped: timeout after {time.time() - t0:.1f}s")
            break

        round_i += 1
        strat = strategies[(round_i - 1) % len(strategies)]
        # --- Placement reseed (skip round 1 so first route uses current layout) ---
        if cfg.do_place and movable and round_i > 1:
            if _time_left() < 2.0:
                stop_reason = "timeout"
                break
            _emit(
                {
                    "event": "stage",
                    "round": round_i,
                    "stage": "place",
                    "strategy": strat["name"],
                    "elapsed_s": time.time() - t0,
                    "best": best_snap.to_dict() if best_snap else None,
                }
            )
            place_cfg = copy.deepcopy(config) if config else PlacementConfig()
            place_cfg.num_candidates = max(1, int(cfg.place_candidates))
            # PlacementConfig enforces sa_iterations >= 100
            place_cfg.sa_iterations = max(100, int(cfg.place_sa_iterations))
            # Diversify SA seeds each round
            place_cfg.random_seed = int(place_cfg.random_seed or 1) + round_i * 997
            # Cap placement time loosely by shrinking iters if little time left
            if _time_left() < 15:
                place_cfg.num_candidates = 1
                place_cfg.sa_iterations = 100
            try:
                place_cfg.use_spice = False
                place_cfg.use_openems = False
                pres = optimize_placement(work, place_cfg)
                apply_positions(work, pres.best.positions)
                placement_cost = float(pres.best.score.total)
                notes.append(
                    f"r{round_i}: place candidate#{pres.best.candidate_id} "
                    f"cost={placement_cost:.3f}"
                )
            except Exception as exc:
                notes.append(f"r{round_i}: place failed ({exc})")

        if not cfg.do_route:
            # Placement-only loop: score geometry as proxy (no route grade)
            elapsed = time.time() - t0
            snap = ImproveSnapshot(
                round=round_i,
                elapsed_s=elapsed,
                strategy="place_only",
                score=0.0,
                grade="F",
                violations=0,
                shorts=0,
                spacing=0,
                outline=0,
                vias=0,
                unrouted=0,
                length_mm=0.0,
                placement_cost=placement_cost,
                stage="place",
                summary=f"placement cost={placement_cost}",
            )
            history.append(snap)
            _emit({"event": "snapshot", **snap.to_dict(), "best": None})
            continue

        # --- Route attempt ---
        _emit(
            {
                "event": "stage",
                "round": round_i,
                "stage": "route",
                "strategy": strat["name"],
                "elapsed_s": time.time() - t0,
                "best": best_snap.to_dict() if best_snap else None,
            }
        )
        try:
            route = _run_route(work, config, strat, cfg, progress_cb)
        except Exception as exc:
            notes.append(f"r{round_i}: route {strat['name']} failed ({exc})")
            continue

        elapsed = time.time() - t0
        # Fast native score first; KiCad oracle when clean-looking or every_round
        snap = _snapshot_from_route(
            route,
            work,
            round_i=round_i,
            elapsed=elapsed,
            strategy=str(strat["name"]),
            stage="scored",
            placement_cost=placement_cost,
            clearance_mm=cfg.clearance_mm,
            cfg=cfg,
            run_kicad=False,
        )
        want_kicad = bool(cfg.pcb_path and cfg.require_kicad_drc) and (
            cfg.kicad_drc_every_round
            or snap.violations <= 8
            or snap.score >= 55
            or round_i == 1
        )
        if want_kicad:
            _emit(
                {
                    "event": "stage",
                    "round": round_i,
                    "stage": "kicad_drc",
                    "strategy": strat["name"],
                    "elapsed_s": time.time() - t0,
                    "best": best_snap.to_dict() if best_snap else None,
                }
            )
            snap = _apply_kicad_to_snapshot(snap, route, cfg, force=True)
            snap.elapsed_s = time.time() - t0

        snap.met_goal = goal_met(snap, cfg)
        history.append(snap)

        is_best = best_snap is None or _route_score_key(snap) > _route_score_key(best_snap)
        if is_best:
            snap.is_best = True
            best_snap = snap
            best_route = route
            best_board = copy.deepcopy(work)
            best_positions = {
                r: (c.x_mm, c.y_mm, c.rotation_deg) for r, c in work.components.items()
            }
            kicad_note = (
                f" kicad={'PASS' if snap.kicad_passed else 'FAIL'}/{snap.kicad_copper_violations}"
                if snap.kicad_available
                else ""
            )
            notes.append(
                f"r{round_i}: NEW BEST {snap.grade}/{snap.score:.0f} "
                f"viol={snap.violations} vias={snap.vias} via {snap.strategy}{kicad_note}"
            )
        else:
            notes.append(
                f"r{round_i}: {snap.grade}/{snap.score:.0f} viol={snap.violations} "
                f"({snap.strategy}) — keep best "
                f"{best_snap.grade if best_snap else '?'}/"
                f"{best_snap.score if best_snap else 0:.0f}"
            )

        _emit(
            {
                "event": "snapshot",
                **snap.to_dict(),
                "is_best": is_best,
                "best": best_snap.to_dict() if best_snap else None,
                "partial": route.to_dict() if hasattr(route, "to_dict") else None,
                "history_len": len(history),
            }
        )

        if snap.met_goal:
            met = True
            stop_reason = "goal"
            notes.append(
                f"goal met: grade {snap.grade} score={snap.score:.1f} "
                f"drc={snap.violations} kicad={snap.kicad_passed} unrouted={snap.unrouted}"
            )
            break

    else:
        stop_reason = "max_rounds"
        notes.append(f"stopped: max_rounds={max_rounds}")

    if stop_reason == "unknown":
        stop_reason = "complete"

    # Ensure best_route has final quality stamped + final KiCad oracle
    if best_route is not None:
        attach_router_drc(best_route, clearance_mm=cfg.clearance_mm, board=best_board)
        best_route.compute_quality()
        if best_snap is not None and cfg.pcb_path and cfg.require_kicad_drc:
            if not best_snap.kicad_available:
                best_snap = _apply_kicad_to_snapshot(
                    best_snap, best_route, cfg, force=True
                )
            else:
                # Preserve the already-run KiCad oracle after the final native
                # pass.  This is the count exposed by /api/jobs and route JSON.
                combined = max(
                    int(best_route.clearance_violations),
                    int(best_snap.violations),
                    int(best_snap.kicad_copper_violations),
                    int(best_snap.kicad_errors),
                )
                best_route.clearance_violations = combined
                quality = best_route.compute_quality()
                best_snap.violations = combined
                best_snap.score = float(quality.get("score") or 0.0)
                best_snap.grade = str(quality.get("grade") or "F")
                best_snap.summary = (
                    f"{quality.get('summary', '')} · KiCad DRC "
                    f"copper={best_snap.kicad_copper_violations} "
                    f"errors={best_snap.kicad_errors} "
                    f"({'PASS' if best_snap.kicad_passed else 'FAIL'})"
                )
        if best_snap:
            best_snap.met_goal = goal_met(best_snap, cfg)
            met = bool(best_snap.met_goal)

    out = ImproveResult(
        board=best_board,
        route=best_route,
        best_snapshot=best_snap,
        history=history,
        stop_reason=stop_reason,
        met_goal=met,
        placement_positions=best_positions,
        notes=notes,
    )
    _emit(
        {
            "event": "done",
            "stop_reason": stop_reason,
            "met_goal": met,
            "best": best_snap.to_dict() if best_snap else None,
            "rounds": len(history),
            "elapsed_s": time.time() - t0,
        }
    )
    return out
