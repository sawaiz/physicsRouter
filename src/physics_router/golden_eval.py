"""Golden-board evaluation: rip human copper → autoroute → score vs human.

Manifest format (YAML/JSON)::

    boards:
      - id: simple_2net
        pcb: tests/fixtures/golden/simple_2net.kicad_pcb
        expect: manufacturing_gate   # or partial_ok
        difficulty: easy
        timeout_s: 30
        rules_profile: via_0p45      # optional A/B
        cbs_repair: true
      - id: halo-90
        pcb: third_party/halo-90/pcb/halo-90.kicad_pcb
        config: examples/halo-90/placement_config.yaml
        expect: partial_ok
        difficulty: hard
        timeout_s: 180
        min_completion: 0.0
"""

from __future__ import annotations

import json
import multiprocessing as mp
import time
import traceback
from pathlib import Path
from typing import Any

import yaml

from physics_router.compare import compare_to_golden, write_golden_markdown
from physics_router.config_io import example_config, load_config
from physics_router.design_rules import (
    apply_rules_profile,
    default_design_rules,
    load_design_rules,
)
from physics_router.kicad_io import load_board_from_kicad_pcb
from physics_router.net_import import import_labels_to_config
from physics_router.router import (
    RouteResult,
    append_routes_to_kicad_pcb,
    extract_routes_from_kicad_pcb,
)


ROOT = Path(__file__).resolve().parents[2]


def load_manifest(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Manifest root must be a mapping: {path}")
    boards = data.get("boards") or []
    if not isinstance(boards, list) or not boards:
        raise ValueError(f"Manifest has no boards: {path}")
    data["boards"] = boards
    data["_manifest_path"] = str(path.resolve())
    return data


def _resolve_path(p: str | Path, *, base: Path) -> Path:
    path = Path(p)
    if path.is_file():
        return path.resolve()
    cand = (base / path).resolve()
    if cand.is_file():
        return cand
    cand2 = (ROOT / path).resolve()
    if cand2.is_file():
        return cand2
    return path


def _load_config_for_pcb(pcb: Path, config_path: Path | None):
    if config_path is not None and config_path.is_file():
        return load_config(config_path)
    base = example_config()
    base.nets = []
    cfg = import_labels_to_config(base, pcb_path=pcb)
    cfg.project_name = pcb.stem
    return cfg


def _autoroute(
    board,
    config,
    rules,
    *,
    pipeline: str,
    effort: float,
    nets_filter: list[str] | None = None,
) -> RouteResult:
    pipe = (pipeline or "auto").lower()
    if pipe in ("capacity", "auto") and nets_filter is None:
        from physics_router.route_pipeline import run_capacity_pipeline

        try:
            return run_capacity_pipeline(
                board,
                config,
                rules,
                effort=float(effort),
                raise_on_fail=False,
                auto_via_profile=True,
            )
        except Exception:
            if pipe == "capacity":
                raise
    from physics_router.routing_strategies import multilayer_route, topor_style_route

    if pipe in ("hybrid", "auto", "multilayer") or rules is not None:
        return multilayer_route(
            board,
            config,
            rules,
            nets_filter=nets_filter,
        )
    return topor_style_route(board, config, rules, nets_filter=nets_filter)


def _route_worker(
    q: Any,
    pcb: str,
    config_path: str | None,
    pipeline: str,
    effort: float,
    rules_profile: str | None,
    manufacturer: str | None,
) -> None:
    """Child process: reload board and route; put RouteResult dict or error."""
    try:
        pcb_p = Path(pcb)
        cfg_p = Path(config_path) if config_path else None
        config = _load_config_for_pcb(pcb_p, cfg_p if cfg_p and cfg_p.is_file() else None)
        board = load_board_from_kicad_pcb(pcb_p, config)
        if manufacturer is None:
            rules = load_design_rules(pcb_path=pcb_p, manufacturer=None)
        else:
            rules = load_design_rules(pcb_path=pcb_p) or default_design_rules()
        if rules_profile:
            rules = apply_rules_profile(rules, rules_profile)
        ar = _autoroute(board, config, rules, pipeline=pipeline, effort=effort)
        q.put({"ok": True, "route": ar.to_dict()})
    except Exception as exc:
        q.put({"ok": False, "error": f"{exc}\n{traceback.format_exc()[-800:]}"})


def autoroute_with_deadline(
    pcb: Path,
    config_path: Path | None,
    *,
    pipeline: str,
    effort: float,
    rules_profile: str | None,
    manufacturer: str | None,
    timeout_s: float,
) -> tuple[RouteResult | None, dict[str, Any]]:
    """Run autoroute in a child process; kill if ``timeout_s`` is exceeded.

    Soft timeouts only *warn* after native search returns. This hard deadline
    terminates hung multipin attempts so CI and corpus runs stay bounded.
    """
    meta: dict[str, Any] = {
        "timeout_s": timeout_s,
        "hard_deadline": True,
        "timed_out": False,
    }
    if timeout_s <= 0:
        # Inline (no process) for tests / debugging
        config = _load_config_for_pcb(pcb, config_path)
        board = load_board_from_kicad_pcb(pcb, config)
        if manufacturer is None:
            rules = load_design_rules(pcb_path=pcb, manufacturer=None)
        else:
            rules = load_design_rules(pcb_path=pcb) or default_design_rules()
        if rules_profile:
            rules = apply_rules_profile(rules, rules_profile)
        ar = _autoroute(board, config, rules, pipeline=pipeline, effort=effort)
        meta["hard_deadline"] = False
        return ar, meta

    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    proc = ctx.Process(
        target=_route_worker,
        args=(
            q,
            str(pcb),
            str(config_path) if config_path else None,
            pipeline,
            effort,
            rules_profile,
            manufacturer,
        ),
    )
    t0 = time.time()
    proc.start()
    proc.join(timeout=float(timeout_s))
    elapsed = time.time() - t0
    meta["elapsed_s"] = round(elapsed, 3)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=5.0)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=2.0)
        meta["timed_out"] = True
        meta["error"] = f"hard deadline {timeout_s:.0f}s exceeded (native search killed)"
        return None, meta
    try:
        payload = q.get_nowait()
    except Exception:
        meta["error"] = "route worker exited without result"
        return None, meta
    if not payload.get("ok"):
        meta["error"] = payload.get("error") or "route worker failed"
        return None, meta
    from physics_router.router import _route_result_from_dict

    return _route_result_from_dict(payload["route"]), meta


def pin_access_metrics(board, rules) -> dict[str, Any]:
    from physics_router.pin_access import build_pin_access_plan

    plan = build_pin_access_plan(board, rules)
    shared = plan.shared_escape_resources()
    return {
        "via_diameter_mm": plan.via_diameter_mm,
        "via_drill_mm": plan.via_drill_mm,
        "clearance_mm": plan.clearance_mm,
        "metrics": dict(plan.metrics),
        "shared_escapes": shared,
    }


def evaluate_board(
    entry: dict[str, Any],
    *,
    pipeline: str = "capacity",
    effort: float = 0.55,
    out_dir: Path | None = None,
    run_route: bool = True,
    run_kicad_drc: bool = False,
    extract_only: bool = False,
    rules_profile: str | None = None,
    hard_deadline: bool = True,
    cbs_repair: bool | None = None,
) -> dict[str, Any]:
    """Rip-and-reroute one golden board and score against human copper.

    Human tracks are **not** used as obstacles (load path ignores copper).
    Zones/pours count as human copper. Autorouter copper is written with
    ``clear_existing_copper=True``.
    """
    board_id = str(entry.get("id") or entry.get("name") or "board")
    manifest_dir = Path(entry.get("_base") or ROOT)
    pcb = _resolve_path(entry["pcb"], base=manifest_dir)
    if not pcb.is_file():
        return {
            "id": board_id,
            "error": f"pcb not found: {entry.get('pcb')}",
            "passed": False,
            "skipped": True,
        }

    cfg_path = None
    if entry.get("config"):
        cfg_path = _resolve_path(entry["config"], base=manifest_dir)

    profile = rules_profile or entry.get("rules_profile")
    use_source = str(profile or "").lower() in ("source", "project", "kicad", "none", "raw")
    auto_profile = str(profile or "").lower() in ("auto", "auto_via", "")

    config = _load_config_for_pcb(pcb, cfg_path)
    board = load_board_from_kicad_pcb(pcb, config)
    if use_source:
        rules = load_design_rules(pcb_path=pcb, manufacturer=None)
    else:
        rules = load_design_rules(pcb_path=pcb) or default_design_rules()
    via_profile_report = None
    if profile and not auto_profile and not use_source:
        rules = apply_rules_profile(rules, profile)
    elif auto_profile or profile is None:
        # Default: physics-based via preflight (also done inside capacity pipeline)
        from physics_router.pin_access import auto_select_via_profile

        rules, via_profile_report = auto_select_via_profile(board, rules)

    human = extract_routes_from_kicad_pcb(pcb, board_nets=board.nets)
    access = pin_access_metrics(board, rules)

    work = out_dir or (ROOT / "viewer" / "runs" / f"golden_{board_id}")
    work.mkdir(parents=True, exist_ok=True)
    human_json = work / "human_route.json"
    human_json.write_text(json.dumps(human.to_dict(), indent=2) + "\n", encoding="utf-8")
    (work / "pin_access.json").write_text(
        json.dumps(access, indent=2) + "\n", encoding="utf-8"
    )

    copper_nets = (
        {s.net for s in human.segments if s.net}
        | {v.net for v in human.vias if v.net}
        | {a.net for a in human.areas if a.net}
    )
    row: dict[str, Any] = {
        "id": board_id,
        "pcb": str(pcb),
        "difficulty": entry.get("difficulty") or "unknown",
        "expect": entry.get("expect") or "partial_ok",
        "rules_profile": (via_profile_report or {}).get("selected")
        if via_profile_report
        else (profile or "default"),
        "via_profile_report": via_profile_report,
        "human": {
            "segments": len(human.segments),
            "vias": human.via_count,
            "areas": len(human.areas or []),
            "length_mm": human.total_length_mm,
            "nets_with_copper": len(copper_nets),
            "zone_nets": len({a.net for a in human.areas if a.net}),
            "unrouted": list(human.unrouted_nets),
        },
        "pin_access": {
            "inner_reachable_anchors": (access.get("metrics") or {}).get(
                "inner_reachable_anchors"
            ),
            "tested_smd_anchors": (access.get("metrics") or {}).get("tested_smd_anchors"),
            "via_diameter_mm": access.get("via_diameter_mm"),
            "via_drill_mm": access.get("via_drill_mm"),
            "shared_escape_savings": (access.get("shared_escapes") or {}).get("savings"),
            "shared_escape_ratio": (access.get("shared_escapes") or {}).get(
                "savings_ratio"
            ),
        },
        "human_json": str(human_json),
        "passed": True,
        "skipped": False,
    }

    if extract_only or not run_route:
        row["mode"] = "extract_only"
        cmp = compare_to_golden(human, human)
        row["comparison"] = {
            "golden_score": cmp.get("golden_score"),
            "golden_grade": cmp.get("golden_grade"),
            "completion": cmp.get("completion"),
        }
        return row

    # Note: timeout_s=0 means inline (no process kill); do not use `or 120`.
    _to = entry.get("timeout_s")
    timeout_s = float(120.0 if _to is None else _to)
    do_hard = hard_deadline and entry.get("hard_deadline", True)
    t0 = time.time()
    deadline_meta: dict[str, Any] = {}
    try:
        if do_hard:
            ar, deadline_meta = autoroute_with_deadline(
                pcb,
                cfg_path,
                pipeline=str(entry.get("pipeline") or pipeline),
                effort=float(
                    entry.get("effort") if entry.get("effort") is not None else effort
                ),
                rules_profile=str(profile) if profile else None,
                manufacturer=None if use_source else "JLCPCB",
                timeout_s=timeout_s,
            )
            if ar is None:
                row["error"] = deadline_meta.get("error") or "route failed"
                row["passed"] = False
                row["timed_out"] = bool(deadline_meta.get("timed_out"))
                row["time_s"] = deadline_meta.get("elapsed_s") or round(
                    time.time() - t0, 3
                )
                row["deadline"] = deadline_meta
                return row
        else:
            ar = _autoroute(
                board,
                config,
                rules,
                pipeline=str(entry.get("pipeline") or pipeline),
                effort=float(
                    entry.get("effort") if entry.get("effort") is not None else effort
                ),
            )
    except Exception as exc:
        row["error"] = f"route failed: {exc}"
        row["passed"] = False
        row["time_s"] = round(time.time() - t0, 3)
        return row
    elapsed = time.time() - t0
    row["time_s"] = round(elapsed, 3)
    row["deadline"] = deadline_meta
    if elapsed > timeout_s and not do_hard:
        row["timeout_warning"] = f"exceeded soft timeout {timeout_s:.0f}s"

    # Optional CBS conflict repair (bounded)
    want_cbs = cbs_repair if cbs_repair is not None else bool(entry.get("cbs_repair", True))
    cbs_log = None
    if want_cbs:
        try:
            from physics_router.conflict_cbs import repair_route_conflicts

            cl = float(rules.constraints.min_clearance_mm or 0.2)
            ar, cbs_log = repair_route_conflicts(
                ar,
                board,
                config,
                clearance_mm=cl,
                max_cluster_size=int(entry.get("cbs_max_cluster") or 6),
                max_clusters=int(entry.get("cbs_max_clusters") or 3),
            )
        except Exception as exc:
            cbs_log = {"error": str(exc)}

    ar_json = work / "ar_route.json"
    ar_json.write_text(json.dumps(ar.to_dict(), indent=2) + "\n", encoding="utf-8")
    ar_pcb = work / f"{board_id}_ar.kicad_pcb"
    append_routes_to_kicad_pcb(
        str(pcb),
        str(ar_pcb),
        ar,
        replace_previous=True,
        clear_existing_copper=True,
    )

    hard_viol = int(ar.clearance_violations or 0)
    q = ar.quality or ar.compute_quality()
    gate = (q or {}).get("manufacturing_gate") or {}
    if isinstance(gate, dict) and gate.get("native_hard_violations") is not None:
        hard_viol = int(gate.get("native_hard_violations") or hard_viol)

    kicad_drc = None
    if run_kicad_drc:
        try:
            from physics_router.kicad_tools import find_kicad_cli, kicad_drc_route

            if find_kicad_cli() is not None:
                kicad_drc = kicad_drc_route(
                    pcb, ar, work_dir=work / "drc", keep_files=True
                )
                if kicad_drc.get("copper_violation_count") is not None:
                    hard_viol = max(
                        hard_viol, int(kicad_drc.get("copper_violation_count") or 0)
                    )
        except Exception as exc:
            kicad_drc = {"error": str(exc)}

    cmp = compare_to_golden(ar, human, hard_violations=hard_viol)
    md_path = work / "golden_compare.md"
    write_golden_markdown(cmp, md_path)
    (work / "golden_compare.json").write_text(
        json.dumps(cmp, indent=2) + "\n", encoding="utf-8"
    )

    # Structured failure / difficulty log for grade improvement
    try:
        from physics_router.route_diagnostics import analyze_route_result, write_diagnostics

        diag = analyze_route_result(
            ar,
            human=human,
            board=board,
            comparison=cmp,
            board_id=board_id,
            extra={
                "pipeline": str(entry.get("pipeline") or pipeline),
                "effort": float(
                    entry.get("effort") if entry.get("effort") is not None else effort
                ),
                "via_profile": (via_profile_report or {}).get("selected")
                if via_profile_report
                else profile,
            },
        )
        diag_paths = write_diagnostics(diag, work, basename="route_diagnostics")
    except Exception as exc:  # noqa: BLE001 — never fail eval on diagnostics
        diag = {"error": str(exc)}
        diag_paths = {}

    completion_ratio = float((cmp.get("completion") or {}).get("ratio") or 0.0)
    min_completion = float(entry.get("min_completion") or 0.0)
    expect = str(entry.get("expect") or "partial_ok").lower()

    passed = hard_viol == 0
    if expect in ("manufacturing_gate", "full", "complete"):
        board_net_names = {n for n in board.nets if n}
        ar_open = set(ar.unrouted_nets or [])
        if ar_open & board_net_names:
            passed = False
        if completion_ratio < 0.999:
            passed = False
        if gate and gate.get("passed") is False:
            passed = False
    elif expect in ("partial_ok", "partial"):
        if completion_ratio + 1e-9 < min_completion:
            passed = False
    else:
        if completion_ratio + 1e-9 < min_completion:
            passed = False

    row.update(
        {
            "ar": {
                "segments": len(ar.segments),
                "vias": ar.via_count,
                "areas": len(ar.areas or []),
                "length_mm": ar.total_length_mm,
                "unrouted": list(ar.unrouted_nets),
                "clearance_violations": ar.clearance_violations,
                "grade": (q or {}).get("grade"),
            },
            "ar_json": str(ar_json),
            "ar_pcb": str(ar_pcb),
            "hard_violations": hard_viol,
            "completion_ratio": completion_ratio,
            "golden_score": cmp.get("golden_score"),
            "golden_grade": cmp.get("golden_grade"),
            "missing_nets": (cmp.get("completion") or {}).get("missing_nets"),
            "missing_zone_pours": (cmp.get("completion") or {}).get("missing_zone_pours"),
            "comparison_md": str(md_path),
            "kicad_drc": kicad_drc,
            "cbs_repair": cbs_log,
            "diagnostics": {
                "summary": (diag or {}).get("summary"),
                "difficulties": [
                    {
                        "id": d.get("id"),
                        "severity": d.get("severity"),
                        "summary": d.get("summary"),
                    }
                    for d in ((diag or {}).get("difficulties") or [])
                ],
                "recommended_actions": (diag or {}).get("recommended_actions"),
                "paths": diag_paths,
            },
            "passed": passed,
            "pipeline": str(entry.get("pipeline") or pipeline),
            "effort": float(
                entry.get("effort") if entry.get("effort") is not None else effort
            ),
        }
    )
    return row


def run_suite(
    manifest_path: str | Path,
    *,
    pipeline: str = "capacity",
    effort: float = 0.55,
    out_dir: Path | None = None,
    board_ids: list[str] | None = None,
    run_kicad_drc: bool = False,
    extract_only: bool = False,
    skip_missing: bool = True,
    rules_profile: str | None = None,
    hard_deadline: bool = True,
    cbs_repair: bool | None = None,
) -> dict[str, Any]:
    """Evaluate every board in a golden manifest."""
    manifest_path = Path(manifest_path)
    manifest = load_manifest(manifest_path)
    base = manifest_path.parent
    suite_out = out_dir or (ROOT / "viewer" / "runs" / "golden_suite")
    suite_out.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for entry in manifest["boards"]:
        eid = str(entry.get("id") or entry.get("name") or "")
        if board_ids is not None and eid not in board_ids:
            continue
        e = dict(entry)
        e["_base"] = str(base)
        board_out = suite_out / eid
        row = evaluate_board(
            e,
            pipeline=pipeline,
            effort=effort,
            out_dir=board_out,
            run_route=not extract_only,
            run_kicad_drc=run_kicad_drc,
            extract_only=extract_only,
            rules_profile=rules_profile,
            hard_deadline=hard_deadline,
            cbs_repair=cbs_repair,
        )
        if row.get("skipped") and not skip_missing:
            row["passed"] = False
        results.append(row)

    n = len(results)
    n_pass = sum(1 for r in results if r.get("passed") and not r.get("skipped"))
    n_skip = sum(1 for r in results if r.get("skipped"))
    n_fail = n - n_pass - n_skip
    summary = {
        "manifest": str(manifest_path.resolve()),
        "pipeline": pipeline,
        "effort": effort,
        "rules_profile": rules_profile,
        "extract_only": extract_only,
        "hard_deadline": hard_deadline,
        "boards": results,
        "counts": {
            "total": n,
            "passed": n_pass,
            "failed": n_fail,
            "skipped": n_skip,
        },
        "passed": n_fail == 0,
    }
    out_json = suite_out / "suite_results.json"
    out_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    summary["out_json"] = str(out_json)
    summary["out_dir"] = str(suite_out)
    return summary
