# Hybrid multi-strategy routing (topological free-angle)

Boards mix dense multipin buses, power, and critical signals. physicsRouter
**auto-classifies nets** and routes each class with free-angle topological
search, painting a **shared obstacle map** so clearance, widths, and layer
policy stay consistent.

Halo-style concentric ring geometry has been **removed**; all strategies use
isotropic free-angle / native A\* with layer striping and vias.

## Strategies

| Strategy | Detection | Tuning |
|----------|-----------|--------|
| **matrix** | CPX-* / MATRIX* / ≥12 pins | Finer grid, layer stripe, vias early |
| **power** | POWER/GND class or VCC/GND names | Wider tracks, plane-prefer layers |
| **critical** | critical / HS / clock / RF / high weight | Fine grid + vias |
| **general** | everything else | Default free-angle / native |

## Paint order

```
matrix → power → critical → general
```

Each phase routes only its nets and seeds prior copper as obstacles.

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
```

`style="auto"|"hybrid"` on `clearance_aware_route`, TopoR when matrix nets
exist, and Improve strategy `"hybrid"`.

Force pure isotropic:

```python
clearance_aware_route(board, config, style="isotropic", skip_hybrid=True)
```
