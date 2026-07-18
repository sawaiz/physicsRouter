"""CLI: place | score | route | import-nets | export-openems | init-config."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from physics_router import __version__
from physics_router.config_io import example_config, load_config, save_config
from physics_router.kicad_io import (
    apply_placement_to_kicad_pcb,
    board_from_synthetic,
    load_board_from_kicad_pcb,
)
from physics_router.placement import optimize_placement, result_to_dict
from physics_router.design_rules import load_design_rules
from physics_router.kicad_tools import (
    export_simulation_bundle,
    export_step,
    find_kicad_cli,
    find_kicad_python,
    render_board_suite,
    run_drc,
    validate_copper_board,
)
from physics_router.router import (
    append_routes_to_kicad_pcb,
    clearance_aware_route,
    rubberband_cleanup,
    topological_guide_route,
)
from physics_router.routing_strategies import (
    escape_hints,
    estimate_via_budget,
    multilayer_route,
    pre_route_analysis,
    topor_style_route,
)


@click.group()
@click.version_option(__version__, prog_name="physics-router")
def main() -> None:
    """Physics-aware KiCad placement and topological routing engine."""


@main.command("init-config")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=Path("placement_config.yaml"),
    help="Where to write the example labeled-net config",
)
def init_config(output: Path) -> None:
    """Write an example placement_config.yaml with labeled nets, weights, and notes."""
    cfg = example_config()
    save_config(cfg, output)
    click.echo(f"Wrote example config to {output}")


@main.command("import-nets")
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--sch", "sch_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option(
    "--project-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Scan directory for all .kicad_sch files",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Existing config to merge into",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=Path("placement_config.yaml"),
)
@click.option("--override/--no-override", default=False, help="Override existing net labels")
def import_nets_cmd(
    pcb_path: Path | None,
    sch_path: Path | None,
    project_dir: Path | None,
    config_path: Path | None,
    output: Path,
    override: bool,
) -> None:
    """Import net labels/weights/notes from KiCad netclasses and schematic fields."""
    from physics_router.net_import import import_labels_to_config

    if not pcb_path and not sch_path and not project_dir:
        raise click.UsageError("Provide --pcb and/or --sch and/or --project-dir")

    base = load_config(config_path) if config_path and config_path.exists() else example_config()
    # start from empty nets if no prior config path
    if config_path is None:
        base.nets = []
        base.notes = "Auto-imported from KiCad netclasses / schematic labels."

    cfg = import_labels_to_config(
        base,
        pcb_path=pcb_path,
        schematic_path=sch_path,
        project_dir=project_dir,
        override=override,
    )
    save_config(cfg, output)
    click.echo(f"Imported {len(cfg.nets)} labeled nets → {output}")
    for n in cfg.nets[:12]:
        click.echo(
            f"  {n.name:16} class={n.net_class.value:12} w={n.weight:.1f} "
            f"crit={n.critical}  {n.notes[:60]}"
        )
    if len(cfg.nets) > 12:
        click.echo(f"  ... and {len(cfg.nets) - 12} more")


@main.command("place")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    required=True,
    help="placement_config.yaml with labeled nets / weights / notes",
)
@click.option(
    "--pcb",
    "pcb_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Input .kicad_pcb (optional; synthetic board if omitted)",
)
@click.option(
    "--out-pcb",
    type=click.Path(path_type=Path),
    default=None,
    help="Write placed .kicad_pcb (requires --pcb)",
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=Path("placement_result.json"),
    help="JSON report of candidates and physics scores",
)
@click.option("--candidates", type=int, default=None, help="Override num_candidates")
@click.option("--iterations", type=int, default=None, help="Override SA iterations")
@click.option("--no-spice/--spice", default=False, help="Disable Ngspice/proxy scoring")
@click.option("--no-openems/--openems", default=False, help="Disable OpenEMS/proxy scoring")
def place_cmd(
    config_path: Path,
    pcb_path: Path | None,
    out_pcb: Path | None,
    out_json: Path,
    candidates: int | None,
    iterations: int | None,
    no_spice: bool,
    no_openems: bool,
) -> None:
    """Optimize component placement using labeled nets and physics simulations."""
    config = load_config(config_path)
    if candidates is not None:
        config.num_candidates = candidates
    if iterations is not None:
        config.sa_iterations = iterations
    if no_spice:
        config.use_spice = False
    if no_openems:
        config.use_openems = False

    if pcb_path is not None:
        board = load_board_from_kicad_pcb(pcb_path, config)
        click.echo(
            f"Loaded board from {pcb_path} "
            f"({len(board.components)} footprints, {len(board.nets)} nets)"
        )
    else:
        board = board_from_synthetic(config)
        click.echo(
            f"Using synthetic demo board ({len(board.components)} parts, {len(board.nets)} nets)"
        )

    result = optimize_placement(board, config)
    report = result_to_dict(result)
    out_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    click.echo(
        f"Best candidate #{result.best.candidate_id} total_score={result.best.score.total:.3f}"
    )
    click.echo(f"  breakdown: {json.dumps(result.best.score.as_dict(), indent=2)}")
    if result.best.score.notes:
        click.echo("  physics notes:")
        for n in result.best.score.notes:
            click.echo(f"    - {n}")
    click.echo(f"Wrote report to {out_json}")

    if out_pcb is not None:
        if pcb_path is None:
            click.echo("Cannot write --out-pcb without --pcb input", err=True)
            sys.exit(2)
        apply_placement_to_kicad_pcb(pcb_path, result.best.positions, out_pcb)
        click.echo(f"Wrote placed PCB to {out_pcb}")


@main.command("score")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), default=None)
def score_cmd(config_path: Path, pcb_path: Path | None) -> None:
    """Score current placement with geometric + physics terms (no optimization)."""
    from physics_router.physics import (
        GeometricSpiceProxy,
        OpenEMSBackend,
        apply_simulation_scores,
        geometric_score,
    )

    config = load_config(config_path)
    board = load_board_from_kicad_pcb(pcb_path, config) if pcb_path else board_from_synthetic(config)
    sb = geometric_score(board, config)
    sb = apply_simulation_scores(
        board,
        config,
        sb,
        spice=GeometricSpiceProxy() if config.use_spice else None,
        openems=OpenEMSBackend() if config.use_openems else None,
    )
    click.echo(json.dumps({"score": sb.as_dict(), "notes": sb.notes}, indent=2))


@main.command("rules")
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--pro", "pro_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
def rules_cmd(pcb_path: Path, pro_path: Path | None, out_json: Path | None) -> None:
    """Dump KiCad stackup, copper layers, and design rules (DRC floors / net classes)."""
    rules = load_design_rules(pcb_path=pcb_path, pro_path=pro_path)
    payload = rules.summary()
    click.echo(json.dumps(payload, indent=2))
    if out_json:
        out_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        click.echo(f"Wrote {out_json}", err=True)


@main.command("pre-route")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), default=None)
def pre_route_cmd(config_path: Path, pcb_path: Path | None) -> None:
    """Run congestion / methodology checks before routing (multilayer advice)."""
    config = load_config(config_path)
    board = load_board_from_kicad_pcb(pcb_path, config) if pcb_path else board_from_synthetic(config)
    rules = load_design_rules(pcb_path=pcb_path) if pcb_path else None
    from physics_router.design_rules import default_design_rules

    rules = rules or default_design_rules()
    report = pre_route_analysis(board, config, rules)
    budget = estimate_via_budget(board, rules, config)
    hints = escape_hints(board, config)
    click.echo(json.dumps({**report.to_dict(), "via_budget": budget, "escape_hints": hints}, indent=2))


@main.command("improve")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--out-json", type=click.Path(path_type=Path), default=Path("improve_result.json"))
@click.option("--timeout", "timeout_s", type=float, default=120.0, help="Seconds to keep trying")
@click.option("--grade", "target_grade", type=click.Choice(["A", "B", "C", "D"], case_sensitive=False), default="A")
@click.option("--min-score", type=float, default=None, help="Override min score (default from grade)")
@click.option("--clearance", type=float, default=0.2)
@click.option("--grid", type=float, default=0.25)
@click.option("--no-place", is_flag=True, help="Route only (skip placement reseeds)")
@click.option("--max-rounds", type=int, default=None)
@click.option("--allow-drc-fail", is_flag=True, help="Do not require zero DRC violations for goal")
def improve_cmd(
    config_path: Path,
    pcb_path: Path | None,
    out_json: Path,
    timeout_s: float,
    target_grade: str,
    min_score: float | None,
    clearance: float,
    grid: float,
    no_place: bool,
    max_rounds: int | None,
    allow_drc_fail: bool,
) -> None:
    """Continuously improve place+route until timeout or grade + full DRC pass."""
    from physics_router.continuous_improve import ImproveConfig, continuous_improve, min_score_for_grade

    config = load_config(config_path)
    board = load_board_from_kicad_pcb(pcb_path, config) if pcb_path else board_from_synthetic(config)
    ms = float(min_score) if min_score is not None else min_score_for_grade(target_grade)
    icfg = ImproveConfig(
        timeout_s=timeout_s,
        min_score=ms,
        target_grade=target_grade.upper(),
        require_drc_clean=not allow_drc_fail,
        require_complete=True,
        do_place=not no_place,
        do_route=True,
        clearance_mm=clearance,
        grid_mm=grid,
        max_rounds=max_rounds,
    )

    def on_prog(ev: dict) -> None:
        if ev.get("event") == "snapshot":
            click.echo(
                f"  r{ev.get('round')} {ev.get('strategy')}: "
                f"{ev.get('grade')}/{ev.get('score')} viol={ev.get('violations')} "
                f"vias={ev.get('vias')} unrouted={ev.get('unrouted')}"
                + (" ★" if ev.get("is_best") else ""),
                err=True,
            )
        elif ev.get("event") == "stage":
            click.echo(
                f"r{ev.get('round')} {ev.get('stage')} · {ev.get('strategy')} "
                f"t={ev.get('elapsed_s', 0):.0f}s",
                err=True,
            )

    click.echo(
        f"Improve timeout={timeout_s:.0f}s target={target_grade} min_score≥{ms:.0f} "
        f"drc_clean={not allow_drc_fail} place={not no_place}"
    )
    result = continuous_improve(board, config, improve=icfg, progress_cb=on_prog)
    payload = result.to_dict()
    if result.route is not None:
        payload["route"] = result.route.to_dict()
        payload["quality"] = result.route.quality
    out_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    best = result.best_snapshot
    click.echo(
        f"stop={result.stop_reason} met_goal={result.met_goal} "
        f"best={best.grade if best else '—'}/{best.score if best else '—'} "
        f"viol={best.violations if best else '—'} rounds={len(result.history)}"
    )
    click.echo(f"Wrote {out_json}")


@main.command("route")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--out-json", type=click.Path(path_type=Path), default=Path("route_result.json"))
@click.option(
    "--out-pcb",
    type=click.Path(path_type=Path),
    default=None,
    help="Append segments/vias into a copy of the input PCB",
)
@click.option(
    "--clearance",
    type=float,
    default=None,
    help="Clearance mm override (default: KiCad min_clearance from board rules)",
)
@click.option("--grid", type=float, default=None, help="Routing grid mm")
@click.option("--no-vias/--vias", default=False, help="Disable vias / multi-layer")
@click.option("--guide-only", is_flag=True, help="Legacy free-angle guide without clearance")
@click.option(
    "--variants",
    type=int,
    default=None,
    help="TopoR multi-variant search count (default: auto by net count, 1–4)",
)
@click.option(
    "--ignore-kicad-rules",
    is_flag=True,
    help="Do not load stackup/DRC from KiCad (use defaults)",
)
@click.option(
    "--drc/--no-drc",
    default=True,
    help="After --out-pcb, run KiCad DRC on the written board (requires kicad-cli)",
)
@click.option(
    "--drc-out",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory for DRC JSON/summary (default: alongside --out-pcb)",
)
def route_cmd(
    config_path: Path,
    pcb_path: Path | None,
    out_json: Path,
    out_pcb: Path | None,
    clearance: float | None,
    grid: float | None,
    no_vias: bool,
    guide_only: bool,
    variants: int | None,
    ignore_kicad_rules: bool,
    drc: bool,
    drc_out: Path | None,
) -> None:
    """Isotropic TopoR-style autorouter (topology → multi-variant → geometry polish)."""
    config = load_config(config_path)
    board = load_board_from_kicad_pcb(pcb_path, config) if pcb_path else board_from_synthetic(config)

    rules = None
    if pcb_path and not ignore_kicad_rules:
        rules = load_design_rules(pcb_path=pcb_path)
        click.echo(
            f"KiCad rules: {len(rules.copper_layers)} copper layers {rules.copper_layers}, "
            f"min_clearance={rules.constraints.min_clearance_mm}mm, "
            f"min_track={rules.constraints.min_track_width_mm}mm"
        )

    if guide_only:
        routes = topological_guide_route(board, config)
    elif rules is not None:
        routes = multilayer_route(
            board,
            config,
            rules,
            clearance_mm=clearance,
            grid_mm=grid,
            allow_vias=not no_vias,
            num_variants=variants,
        )
    else:
        routes = topor_style_route(
            board,
            config,
            None,
            clearance_mm=clearance if clearance is not None else 0.2,
            grid_mm=grid,
            allow_vias=not no_vias,
            num_variants=variants,
        )

    out_json.write_text(json.dumps(routes.to_dict(), indent=2) + "\n", encoding="utf-8")
    q = routes.quality or routes.compute_quality()
    click.echo(
        f"Routed: {len(routes.segments)} segments, {routes.via_count} vias, "
        f"{routes.total_length_mm:.2f} mm, unrouted={len(routes.unrouted_nets)} · "
        f"grade {q.get('grade')} ({q.get('score')}/100)"
    )
    if q.get("winner"):
        click.echo(f"  TopoR winner variant: {q.get('winner')}")
    if routes.notes:
        for n in routes.notes[:12]:
            click.echo(f"  note: {n}")
    if routes.unrouted_nets:
        click.echo(f"  unrouted nets: {', '.join(routes.unrouted_nets[:20])}")
    click.echo(f"Wrote {out_json}")

    if out_pcb is not None:
        if pcb_path is None:
            click.echo("Cannot write --out-pcb without --pcb", err=True)
            sys.exit(2)
        append_routes_to_kicad_pcb(str(pcb_path), str(out_pcb), routes)
        click.echo(f"Wrote routed PCB to {out_pcb}")
        if drc:
            if find_kicad_cli() is None:
                click.echo("kicad-cli not found — skip DRC (set KICAD_CLI)", err=True)
            else:
                ddir = drc_out or (out_pcb.parent / f"{out_pcb.stem}_drc")
                summary = validate_copper_board(out_pcb, ddir)
                click.echo(
                    f"KiCad DRC: errors={summary['error_count']} "
                    f"warnings={summary['warning_count']} "
                    f"copper_issues={summary['copper_violation_count']} "
                    f"passed={summary['passed']}"
                )
                click.echo(f"  report → {ddir / 'drc.json'}")
                top = list(summary.get("by_type", {}).items())[:8]
                if top:
                    click.echo("  top issues: " + ", ".join(f"{k}={v}" for k, v in top))


@main.command("drc")
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Output directory for drc.json + summary",
)
@click.option("--refill-zones", is_flag=True, help="Refill zones before DRC")
def drc_cmd(pcb_path: Path, out_dir: Path | None, refill_zones: bool) -> None:
    """Run official KiCad DRC (kicad-cli) and summarize copper violations."""
    if find_kicad_cli() is None:
        raise click.ClickException("kicad-cli not found. Install KiCad or set KICAD_CLI.")
    out_dir = out_dir or Path("drc_out")
    out_dir.mkdir(parents=True, exist_ok=True)
    report = run_drc(pcb_path, out_dir / "drc.json", refill_zones=refill_zones)
    summary = report.to_dict()
    (out_dir / "drc_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    click.echo(json.dumps(summary, indent=2))
    if not report.passed:
        sys.exit(2)


@main.command("render")
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path),
    default=Path("kicad_renders"),
    help="Output directory for SVG plots and 3D PNGs",
)
@click.option(
    "--layers",
    default="F.Cu,B.Cu,In1.Cu,In2.Cu,Edge.Cuts,F.SilkS",
    help="Comma-separated KiCad layer names",
)
@click.option("--no-pcbnew", is_flag=True, help="Skip direct pcbnew PLOT_CONTROLLER path")
def render_cmd(pcb_path: Path, out_dir: Path, layers: str, no_pcbnew: bool) -> None:
    """Render board with official KiCad tools (kicad-cli SVG/3D + optional pcbnew)."""
    if find_kicad_cli() is None and find_kicad_python() is None:
        raise click.ClickException("Neither kicad-cli nor KiCad Python/pcbnew found.")
    layer_list = [x.strip() for x in layers.split(",") if x.strip()]
    result = render_board_suite(
        pcb_path,
        out_dir,
        use_pcbnew=not no_pcbnew,
        layers=layer_list,
    )
    click.echo(json.dumps(result, indent=2))


@main.command("export-step")
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Output .step path (default: <board>_sim.step)",
)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="If set, write full simulation STEP bundle (tracks/pads/mask/silk)",
)
@click.option(
    "--net-filter",
    default="",
    help="Optional KiCad net wildcard for copper-only STEP (e.g. 'CPX*')",
)
@click.option("--with-components/--board-only", default=False, help="Include footprint 3D models")
def export_step_cmd(
    pcb_path: Path,
    output: Path | None,
    out_dir: Path | None,
    net_filter: str,
    with_components: bool,
) -> None:
    """Export STEP with copper tracks, pads, soldermask, silkscreen for OpenEMS/FEM."""
    if find_kicad_cli() is None:
        raise click.ClickException("kicad-cli not found")
    if out_dir is not None:
        result = export_simulation_bundle(
            pcb_path,
            out_dir,
            nets_filter=net_filter,
            board_only=not with_components,
        )
        click.echo(json.dumps(result, indent=2))
        return
    output = output or Path(f"{pcb_path.stem}_sim.step")
    path = export_step(
        pcb_path,
        output,
        board_only=not with_components,
        no_components=not with_components,
        include_tracks=True,
        include_pads=True,
        include_zones=True,
        include_inner_copper=True,
        include_silkscreen=True,
        include_soldermask=True,
        net_filter=net_filter,
    )
    click.echo(f"Wrote STEP → {path} ({path.stat().st_size} bytes)")


@main.command("route-guide")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--out-json", type=click.Path(path_type=Path), default=Path("route_guide.json"))
def route_guide_cmd(config_path: Path, pcb_path: Path | None, out_json: Path) -> None:
    """Emit free-angle topological guide routes (no clearance)."""
    config = load_config(config_path)
    board = load_board_from_kicad_pcb(pcb_path, config) if pcb_path else board_from_synthetic(config)
    routes = topological_guide_route(board, config)
    out_json.write_text(json.dumps(routes.to_dict(), indent=2) + "\n", encoding="utf-8")
    click.echo(
        f"Guide route: {len(routes.segments)} segments, "
        f"{routes.total_length_mm:.2f} mm, via_proxy={routes.via_count}"
    )
    click.echo(f"Wrote {out_json}")


@main.command("export-openems")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option(
    "--gerber",
    "gerbers",
    multiple=True,
    type=str,
    help="layer=path pairs, e.g. F.Cu=front.gbr (repeatable)",
)
@click.option(
    "--out-dir",
    type=click.Path(path_type=Path),
    default=Path("openems_export"),
    help="Output directory for geometry + simulate_board.py",
)
@click.option("--route/--no-route", default=True, help="Run clearance-aware route before export")
@click.option("--f0", type=float, default=1e9, help="Gaussian excite center Hz")
@click.option("--fc", type=float, default=1e9, help="Gaussian excite width Hz")
def export_openems_cmd(
    config_path: Path | None,
    pcb_path: Path | None,
    gerbers: tuple[str, ...],
    out_dir: Path,
    route: bool,
    f0: float,
    fc: float,
) -> None:
    """Export OpenEMS mesh geometry from placement/routes and/or Gerbers."""
    from physics_router.openems_export import export_openems_bundle

    config = load_config(config_path) if config_path else example_config()
    board = None
    routes = None
    if pcb_path or config_path:
        board = (
            load_board_from_kicad_pcb(pcb_path, config)
            if pcb_path
            else board_from_synthetic(config)
        )
        if route:
            routes = clearance_aware_route(board, config, clearance_mm=0.2)

    gerber_paths: dict[str, str] = {}
    for item in gerbers:
        if "=" not in item:
            raise click.UsageError(f"--gerber expects layer=path, got {item!r}")
        layer, path = item.split("=", 1)
        gerber_paths[layer] = path

    if board is None and not gerber_paths:
        raise click.UsageError("Provide --pcb/--config and/or --gerber layer=path")

    # Prefer EMI-sensitive nets if labeled
    nets_filter = None
    if config.nets and any(n.simulate_em or n.emi_sensitive for n in config.nets):
        nets_filter = {
            n.name for n in config.nets if n.simulate_em or n.emi_sensitive or n.critical
        }

    design_rules = load_design_rules(pcb_path=pcb_path) if pcb_path else None
    paths = export_openems_bundle(
        out_dir,
        board=board,
        routes=routes,
        config=config,
        gerber_paths=gerber_paths or None,
        nets_filter=nets_filter,
        f0_hz=f0,
        fc_hz=fc,
        design_rules=design_rules,
    )
    click.echo(f"OpenEMS export → {out_dir}")
    for k, p in paths.items():
        click.echo(f"  {k}: {p}")


@main.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
def serve_cmd(host: str, port: int) -> None:
    """Interactive control plane: config, jobs, progress, board viewer, tests."""
    from physics_router.server import serve

    serve(host=host, port=port)


@main.command("export-dsn")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("-o", "--output", type=click.Path(path_type=Path), default=Path("board.dsn"))
def export_dsn_cmd(config_path: Path | None, pcb_path: Path | None, output: Path) -> None:
    """Export Specctra DSN for FreeRouting baseline autorouting."""
    from physics_router.dsn_export import export_dsn, write_freerouting_readme

    config = load_config(config_path) if config_path else example_config()
    board = load_board_from_kicad_pcb(pcb_path, config) if pcb_path else board_from_synthetic(config)
    rules = load_design_rules(pcb_path) if pcb_path else None
    path = export_dsn(board, output, config=config, rules=rules)
    write_freerouting_readme(output.parent)
    click.echo(f"Wrote DSN → {path}")
    click.echo(f"FreeRouting notes → {output.parent / 'FREEROUTING.md'}")


@main.command("compare-routes")
@click.option("--topor", "topor_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--baseline-json", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--ses", "ses_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--out", "out_json", type=click.Path(path_type=Path), default=Path("comparison.json"))
@click.option("--md", "out_md", type=click.Path(path_type=Path), default=Path("comparison.md"))
def compare_routes_cmd(
    topor_path: Path,
    baseline_json: Path | None,
    ses_path: Path | None,
    out_json: Path,
    out_md: Path,
) -> None:
    """Side-by-side TopoR vs FreeRouting (SES or JSON) metrics."""
    from physics_router.compare import (
        compare_metrics,
        load_route_metrics,
        parse_ses_metrics,
        write_comparison_markdown,
    )

    topor = load_route_metrics(topor_path)
    baseline = None
    if baseline_json:
        baseline = load_route_metrics(baseline_json)
    elif ses_path:
        baseline = parse_ses_metrics(ses_path)
    cmp = compare_metrics(topor, baseline)
    out_json.write_text(json.dumps(cmp, indent=2) + "\n", encoding="utf-8")
    write_comparison_markdown(cmp, out_md)
    click.echo(json.dumps(cmp.get("winner") or cmp.get("notes"), indent=2))
    click.echo(f"Wrote {out_json} and {out_md}")


@main.command("dashboard")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--viewer-data", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("-o", "--output", type=click.Path(path_type=Path), default=Path("dashboard.html"))
@click.option("--viewer-url", default="viewer/index.html")
def dashboard_cmd(
    config_path: Path | None,
    pcb_path: Path | None,
    viewer_data: Path | None,
    output: Path,
    viewer_url: str,
) -> None:
    """Write HTML physics-budget dashboard (score, IR, EMI, routes)."""
    from physics_router.dashboard import write_dashboard
    from physics_router.physics import (
        GeometricSpiceProxy,
        OpenEMSBackend,
        apply_simulation_scores,
        geometric_score,
    )

    routes = {}
    comparison = None
    board_meta = {}
    if viewer_data:
        payload = json.loads(Path(viewer_data).read_text(encoding="utf-8"))
        physics = payload.get("physics") or {}
        routes = payload.get("routes") or {}
        comparison = payload.get("comparison")
        b = payload.get("board") or {}
        board_meta = {
            "components": len(b.get("components") or []),
            "nets": len(b.get("nets") or {}),
            "layers": ", ".join(b.get("copper_layers") or []),
        }
    else:
        config = load_config(config_path) if config_path else example_config()
        board = load_board_from_kicad_pcb(pcb_path, config) if pcb_path else board_from_synthetic(config)
        sb = geometric_score(board, config)
        sb = apply_simulation_scores(
            board, config, sb, spice=GeometricSpiceProxy(), openems=OpenEMSBackend()
        )
        physics = {"score": sb.as_dict(), "notes": sb.notes}
        board_meta = {
            "components": len(board.components),
            "nets": len(board.nets),
            "layers": ", ".join(board.copper_layers),
        }
    write_dashboard(
        output,
        physics,
        title="Physics budget",
        board_meta=board_meta,
        routes=routes,
        comparison=comparison,
        viewer_url=viewer_url,
    )
    click.echo(f"Wrote dashboard → {output}")


@main.command("viewer-data")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("-o", "--output", type=click.Path(path_type=Path), default=Path("viewer_data.json"))
@click.option("--route-json", multiple=True, help="name=path.json for additional route variants")
def viewer_data_cmd(
    config_path: Path | None,
    pcb_path: Path | None,
    output: Path,
    route_json: tuple[str, ...],
) -> None:
    """Export viewer_data.json for the interactive three.js viewer."""
    from physics_router.router import RouteResult, RouteSegment, Via
    from physics_router.viewer_export import build_viewer_payload, write_viewer_data

    config = load_config(config_path) if config_path else example_config()
    board = load_board_from_kicad_pcb(pcb_path, config) if pcb_path else board_from_synthetic(config)
    routes = {"guide": topological_guide_route(board, config)}
    for item in route_json:
        if "=" not in item:
            raise click.UsageError("--route-json expects name=path")
        name, path = item.split("=", 1)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
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
            for s in data.get("segments") or []
        ]
        vias = [
            Via(
                x=v["x"],
                y=v["y"],
                net=v.get("net", ""),
                size_mm=v.get("size_mm", 0.8),
                drill_mm=v.get("drill_mm", 0.4),
            )
            for v in data.get("vias") or []
        ]
        routes[name] = RouteResult(
            segments=segs,
            vias=vias,
            via_count=int(data.get("via_count") or len(vias)),
            total_length_mm=float(data.get("total_length_mm") or 0),
            unrouted_nets=list(data.get("unrouted_nets") or []),
            clearance_violations=int(data.get("clearance_violations") or 0),
            notes=list(data.get("notes") or []),
        )
    payload = build_viewer_payload(board, config, routes=routes)
    write_viewer_data(payload, output)
    click.echo(f"Wrote {output} ({len(routes)} route variants)")


if __name__ == "__main__":
    main()
