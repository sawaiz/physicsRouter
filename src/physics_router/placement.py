"""Multi-candidate placement optimizer with physics-in-the-loop ranking."""

from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass

from physics_router.models import (
    BoardModel,
    PlacementCandidate,
    PlacementConfig,
    ScoreBreakdown,
)
from physics_router.physics import (
    GeometricSpiceProxy,
    OpenEMSBackend,
    apply_simulation_scores,
    geometric_score,
)


@dataclass
class PlacementResult:
    best: PlacementCandidate
    candidates: list[PlacementCandidate]
    board: BoardModel


def _snapshot_positions(board: BoardModel) -> dict[str, tuple[float, float, float]]:
    return {
        r: (c.x_mm, c.y_mm, c.rotation_deg) for r, c in board.components.items()
    }


def apply_positions(board: BoardModel, positions: dict[str, tuple[float, float, float]]) -> None:
    for ref, (x, y, rot) in positions.items():
        if ref not in board.components:
            continue
        c = board.components[ref]
        if c.locked:
            continue
        c.x_mm, c.y_mm, c.rotation_deg = x, y, rot


def _clamp(board: BoardModel, ref: str) -> None:
    c = board.components[ref]
    margin = max(c.width_mm, c.height_mm) / 2
    c.x_mm = min(max(c.x_mm, margin), board.width_mm - margin)
    c.y_mm = min(max(c.y_mm, margin), board.height_mm - margin)


def _seed_placement(board: BoardModel, config: PlacementConfig, rng: random.Random) -> None:
    """Region-aware random seed for movable parts."""
    for ref in board.movable_refs():
        c = board.components[ref]
        # Prefer regions listing this ref
        region = next((r for r in config.regions if ref in r.preferred_refs), None)
        if region:
            c.x_mm = rng.uniform(region.x_min_mm, region.x_max_mm)
            c.y_mm = rng.uniform(region.y_min_mm, region.y_max_mm)
        else:
            c.x_mm = rng.uniform(2.0, max(3.0, board.width_mm - 2.0))
            c.y_mm = rng.uniform(2.0, max(3.0, board.height_mm - 2.0))
        c.rotation_deg = rng.choice([0.0, 90.0, 180.0, 270.0])
        _clamp(board, ref)


def _sa_optimize(
    board: BoardModel,
    config: PlacementConfig,
    rng: random.Random,
    iterations: int,
) -> ScoreBreakdown:
    """Simulated annealing minimizing geometric multi-objective cost."""
    current = geometric_score(board, config)
    best_score = current.total
    best_pos = _snapshot_positions(board)
    temp = config.sa_initial_temp
    movable = board.movable_refs()
    if not movable:
        return current

    for _ in range(iterations):
        ref = rng.choice(movable)
        c = board.components[ref]
        old = (c.x_mm, c.y_mm, c.rotation_deg)
        other_ref: str | None = None
        other_old: tuple[float, float, float] | None = None

        move = rng.random()
        if move < 0.7:
            step = max(config.grid_mm, temp * 2.0)
            c.x_mm += rng.uniform(-step, step)
            c.y_mm += rng.uniform(-step, step)
            g = config.grid_mm
            c.x_mm = round(c.x_mm / g) * g
            c.y_mm = round(c.y_mm / g) * g
        elif move < 0.9:
            c.rotation_deg = (c.rotation_deg + 90.0) % 360.0
        else:
            other_ref = rng.choice(movable)
            if other_ref != ref:
                o = board.components[other_ref]
                other_old = (o.x_mm, o.y_mm, o.rotation_deg)
                c.x_mm, o.x_mm = o.x_mm, c.x_mm
                c.y_mm, o.y_mm = o.y_mm, c.y_mm
                _clamp(board, other_ref)
        _clamp(board, ref)

        new_score = geometric_score(board, config)
        delta = new_score.total - current.total
        if delta < 0 or rng.random() < math.exp(-delta / max(temp, 1e-9)):
            current = new_score
            if current.total < best_score:
                best_score = current.total
                best_pos = _snapshot_positions(board)
        else:
            c.x_mm, c.y_mm, c.rotation_deg = old
            if other_ref is not None and other_old is not None:
                o = board.components[other_ref]
                o.x_mm, o.y_mm, o.rotation_deg = other_old

        temp *= config.sa_cooling

    apply_positions(board, best_pos)
    return geometric_score(board, config)


def optimize_placement(
    board: BoardModel,
    config: PlacementConfig,
    *,
    spice_backend: GeometricSpiceProxy | None = None,
    openems_backend: OpenEMSBackend | None = None,
) -> PlacementResult:
    """Generate multiple placement candidates; rank with geometry then physics sims."""
    spice = spice_backend or GeometricSpiceProxy()
    openems = openems_backend or OpenEMSBackend()

    base_board = copy.deepcopy(board)
    raw_candidates: list[PlacementCandidate] = []

    for i in range(config.num_candidates):
        b = copy.deepcopy(base_board)
        # diversify seeds
        _seed_placement(b, config, random.Random(config.random_seed + i * 997))
        _sa_optimize(b, config, random.Random(config.random_seed + i * 13), config.sa_iterations)
        score = geometric_score(b, config)
        raw_candidates.append(
            PlacementCandidate(
                candidate_id=i,
                positions=_snapshot_positions(b),
                score=score,
            )
        )

    # Physics proxies on every candidate (cheap, fair ranking).
    # Real ngspice binary path is inside GeometricSpiceProxy when available.
    for cand in raw_candidates:
        b = copy.deepcopy(base_board)
        apply_positions(b, cand.positions)
        sb = geometric_score(b, config)
        sb = apply_simulation_scores(
            b,
            config,
            sb,
            spice=spice if config.use_spice else None,
            openems=openems if config.use_openems else None,
        )
        cand.score = sb

    # Prefer lower total; break ties with fewer overlap violations
    raw_candidates.sort(key=lambda c: (c.score.total, c.score.overlap_penalty))
    for rank, cand in enumerate(raw_candidates, start=1):
        cand.rank = rank

    best = raw_candidates[0]
    out_board = copy.deepcopy(base_board)
    apply_positions(out_board, best.positions)

    return PlacementResult(best=best, candidates=raw_candidates, board=out_board)


def result_to_dict(result: PlacementResult) -> dict:
    return {
        "best_candidate_id": result.best.candidate_id,
        "best_score": result.best.score.as_dict(),
        "best_notes": result.best.score.notes,
        "best_positions": {
            ref: {"x_mm": p[0], "y_mm": p[1], "rotation_deg": p[2]}
            for ref, p in result.best.positions.items()
        },
        "candidates": [
            {
                "id": c.candidate_id,
                "rank": c.rank,
                "score": c.score.as_dict(),
                "notes": c.score.notes,
            }
            for c in result.candidates
        ],
    }
