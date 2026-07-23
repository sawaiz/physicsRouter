"""Golden-board evaluation: rip human copper → autoroute → score vs human.

Manifest format (YAML/JSON)::

    boards:
      - id: simple_2net
        pcb: tests/fixtures/golden/simple_2net.kicad_pcb
        expect: manufacturing_gate   # or partial_ok
        difficulty: easy
        timeout_s: 30
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
import time
from pathlib import Path
from typing import Any

import yaml

from physics_router.compare import compare_to_golden, write_golden_markdown
from physics_router.config_io import example_config, load_config
from physics_router.design_rules import default_design_rules, load_design_rules
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
                board, config, rules, effort=float(effort), raise_on_fail=False
            )
        except Exception:
            if pipe == "capacity":
                raise
            # fall through for auto
    from physics_router.routing_strategies import multilayer_route, topor_style_route

    if pipe in ("hybrid", "auto", "multilayer") or rules is not None:
        return multilayer_route(
            board,
            config,
            rules,
            nets_filter=nets_filter,
        )
    return topor_style_route(board, config, rules, nets_filter=nets_filter)


def evaluate_board(
    entry: dict[str, Any],
    *,
    pipeline: str = "capacity",
    effort: float = 0.55,
    out_dir: Path | None = None,
    run_route: bool = True,
    run_kicad_drc: bool = False,
    extract_only: bool = False,
) -> dict[str, Any]:
    """Rip-and-reroute one golden board and score against human copper.

    Human tracks are **not** used as obstacles (load path ignores copper).
    Autorouter copper is written with ``clear_existing_copper=True``.
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

    config = _load_config_for_pcb(pcb, cfg_path)
    board = load_board_from_kicad_pcb(pcb, config)
    rules = load_design_rules(pcb_path=pcb) or default_design_rules()

    human = extract_routes_from_kicad_pcb(pcb, board_nets=board.nets)

    work = out_dir or (ROOT / "viewer" / "runs" / f"golden_{board_id}")
    work.mkdir(parents=True, exist_ok=True)
    human_json = work / "human_route.json"
    human_json.write_text(json.dumps(human.to_dict(), indent=2) + "\n", encoding="utf-8")

    row: dict[str, Any] = {
        "id": board_id,
        "pcb": str(pcb),
        "difficulty": entry.get("difficulty") or "unknown",
        "expect": entry.get("expect") or "partial_ok",
        "human": {
            "segments": len(human.segments),
            "vias": human.via_count,
            "length_mm": human.total_length_mm,
            "nets_with_copper": len(
                {s.net for s in human.segments if s.net}
                | {v.net for v in human.vias if v.net}
            ),
            "unrouted": list(human.unrouted_nets),
        },
        "human_json": str(human_json),
        "passed": True,
        "skipped": False,
    }

    if extract_only or not run_route:
        row["mode"] = "extract_only"
        cmp = compare_to_golden(human, human)  # identity
        row["comparison"] = {
            "golden_score": cmp.get("golden_score"),
            "golden_grade": cmp.get("golden_grade"),
            "completion": cmp.get("completion"),
        }
        return row

    timeout_s = float(entry.get("timeout_s") or 120.0)
    t0 = time.time()
    try:
        ar = _autoroute(
            board,
            config,
            rules,
            pipeline=str(entry.get("pipeline") or pipeline),
            effort=float(entry.get("effort") if entry.get("effort") is not None else effort),
        )
    except Exception as exc:
        row["error"] = f"route failed: {exc}"
        row["passed"] = False
        row["time_s"] = round(time.time() - t0, 3)
        return row
    elapsed = time.time() - t0
    row["time_s"] = round(elapsed, 3)
    if elapsed > timeout_s:
        row["timeout_warning"] = f"exceeded soft timeout {timeout_s:.0f}s"

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

    completion_ratio = float((cmp.get("completion") or {}).get("ratio") or 0.0)
    min_completion = float(entry.get("min_completion") or 0.0)
    expect = str(entry.get("expect") or "partial_ok").lower()

    passed = hard_viol == 0
    if expect in ("manufacturing_gate", "full", "complete"):
        # All board nets complete + zero hard DRC
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
        # custom: only hard DRC + optional min_completion
        if completion_ratio + 1e-9 < min_completion:
            passed = False

    row.update(
        {
            "ar": {
                "segments": len(ar.segments),
                "vias": ar.via_count,
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
            "comparison_md": str(md_path),
            "kicad_drc": kicad_drc,
            "passed": passed,
            "pipeline": str(entry.get("pipeline") or pipeline),
            "effort": float(entry.get("effort") if entry.get("effort") is not None else effort),
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
        "extract_only": extract_only,
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
