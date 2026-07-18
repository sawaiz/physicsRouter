# Hybrid multi-strategy routing (topological free-angle)

Boards mix dense multipin buses, power, and critical signals. physicsRouter
**auto-classifies nets** and routes each class with free-angle topological
search, painting a **shared obstacle map** so clearance, widths, and layer
policy stay consistent. Each bucket is attempted as an atomic native batch:
an incomplete multipin net is rolled back rather than leaking misleading
partial copper.

Halo-style concentric ring geometry has been **removed**; all strategies use
isotropic free-angle / native A\* with layer striping and vias.

## Strategies

| Strategy | Detection | Tuning |
|----------|-----------|--------|
| **matrix** | CPX-* / MATRIX* / ≥12 pins | Finer grid, layer stripe, vias early |
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
parallel and keep the most-complete legal result.

HALO-90 v1.7 legally completes two of ten CPX nets; eight remain intentionally
open. Overall completion is 9/23 with 21 explicit vias and one GND area. This
stricter result enforces the physical copper layers exposed by every pad; the
previous 17/23 snapshot incorrectly accepted inner-layer endpoints at F.Cu-only
SMD pads. Solving the rest needs a true concurrent bundle/topology search.

## Constraints

| Constraint | Source |
|------------|--------|
| Clearance | DesignRules / KiCad net classes |
| Track width | `track_width_for_net` (+ power boost) |
| Layers | `layers_for_net` + CPX/matrix stripe |
| Priority | PlacementConfig weights |

## API

```python
from physics_router.hybrid_route import classify_board, hybrid_route

plan = classify_board(board, config, rules)
result = hybrid_route(board, config, rules)
# result.quality["hybrid_plan"]
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
