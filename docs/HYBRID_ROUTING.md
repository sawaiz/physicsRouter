# Hybrid multi-strategy routing (topological free-angle)

Boards mix dense multipin buses, power, and critical signals. physicsRouter
**auto-classifies nets** and routes each class with free-angle topological
search, painting a **shared obstacle map** so clearance, widths, and layer
policy stay consistent. Each bucket is attempted as an atomic native batch:
an incomplete multipin net is rolled back rather than leaking misleading
partial copper.

Halo-style concentric ring geometry has been **removed**; all strategies use
isotropic free-angle / native A\* with graph-colored layers and vias.

## Strategies

| Strategy | Detection | Tuning |
|----------|-----------|--------|
| **matrix** | CPX-* / MATRIX* / ≥12 pins | Finer grid, DSATUR-preferred layer, vias early |
| **power** | POWER/GND class or VCC/GND names | Rounded native copper areas on plane-preferred layers; tracks when needed |
| **critical** | critical / HS / clock / RF / high weight | Fine grid + vias |
| **general** | everything else | Default free-angle / native |

## Paint order

```
power → critical → matrix → general
```

Power areas reserve the intended planes first. Each later phase routes only
its nets and seeds prior tracks and vias as physical-width obstacles; KiCad
remains the area-fill authority. The exact native DRC gate rejects a batch if
it introduces track/via shorts, spacing hits, or an Edge.Cuts escape. Power,
critical, and matrix buckets compare deterministic whole-bucket rebuilds in
parallel and keep the most-complete legal result. A final board-wide
PathFinder-style pass routes temporary per-net candidates with sparse present
and historical cell costs. Exact native DRC markers define the conflict graph;
legalization retains deterministic maximal independent sets and sequentially
retries only removed or incomplete victims.

HALO-90 v1.9.1 legally completes two of ten CPX nets; eight remain intentionally
open. Overall completion is 12/23. The first negotiation round finds 21
complete temporary candidates, but they have
more than 2,000 exact conflicts and are never exposed as legal copper. Three
bounded rounds reduce coarse overused cells from 2,825 to 1,163 before exact
legalization and targeted repair. Solving the rest needs conflict-component
route alternatives and section-level layer/via assignment, including legal
offset escape vias around the dense 0402 pads, not a weaker DRC gate. The
current legal selection has zero vias; the earlier 42-via checkpoint contained
33 same-net or foreign via/pad violations under the corrected rule.

## Constraints

| Constraint | Source |
|------------|--------|
| Clearance | DesignRules / KiCad net classes |
| Track width | `track_width_for_net` (+ power boost) |
| Layers | `layers_for_net` + conflict-graph DSATUR preference |
| Priority | PlacementConfig weights |

## API

```python
from physics_router.hybrid_route import classify_board, hybrid_route

plan = classify_board(board, config, rules)
result = hybrid_route(board, config, rules)
# result.quality["hybrid_plan"]
# result.quality["graph_topology"]  # components, cycles, crossings, cuts
# result.areas          # refillable power/ground geometry
# result.unrouted_nets  # honest atomic failures
```

`style="auto"|"hybrid"` on `clearance_aware_route`, TopoR when matrix nets
exist, and Improve strategy `"hybrid"`.

Force pure isotropic:

```python
clearance_aware_route(board, config, style="isotropic", skip_hybrid=True)
```

KiCad owns zone refill, thermals, and fabrication DRC. Native DRC validates
the emitted area boundary and all committed track/via geometry, but does not
pretend an unfilled zone outline is the final copper polygon.
