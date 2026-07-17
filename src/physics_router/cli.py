"""CLI: physics-router place | score | init-config | route-guide."""

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
from physics_router.router import topological_guide_route


@click.group()
@click.version_option(__version__, prog_name="physics-router")
def main() -> None:
    """Physics-aware KiCad placement and routing engine."""


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
        click.echo(f"Loaded board from {pcb_path} ({len(board.components)} footprints, {len(board.nets)} nets)")
    else:
        board = board_from_synthetic(config)
        click.echo(f"Using synthetic demo board ({len(board.components)} parts, {len(board.nets)} nets)")

    result = optimize_placement(board, config)
    report = result_to_dict(result)
    out_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    click.echo(f"Best candidate #{result.best.candidate_id} total_score={result.best.score.total:.3f}")
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


@main.command("route-guide")
@click.option("--config", "config_path", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--pcb", "pcb_path", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--out-json", type=click.Path(path_type=Path), default=Path("route_guide.json"))
def route_guide_cmd(config_path: Path, pcb_path: Path | None, out_json: Path) -> None:
    """Emit free-angle topological guide routes for the current placement."""
    config = load_config(config_path)
    board = load_board_from_kicad_pcb(pcb_path, config) if pcb_path else board_from_synthetic(config)
    routes = topological_guide_route(board, config)
    payload = {
        "total_length_mm": routes.total_length_mm,
        "via_count_proxy": routes.via_count,
        "unrouted_nets": routes.unrouted_nets,
        "segments": [
            {
                "net": s.net,
                "x1": s.x1,
                "y1": s.y1,
                "x2": s.x2,
                "y2": s.y2,
                "layer": s.layer,
                "width_mm": s.width_mm,
            }
            for s in routes.segments
        ],
    }
    out_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    click.echo(
        f"Guide route: {len(routes.segments)} segments, "
        f"{routes.total_length_mm:.2f} mm, via_proxy={routes.via_count}"
    )
    click.echo(f"Wrote {out_json}")


if __name__ == "__main__":
    main()
