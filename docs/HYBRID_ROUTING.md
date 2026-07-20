# Hybrid multi-strategy routing

**TL;DR:** Nets are bucketed (matrix / power / critical / general), each bucket free-angle-routed against a **shared** clearance map. Incomplete multipin nets roll back — no stub copper. No special HALO ring corridor anymore.

| Strategy | Detection | Tuning |
|----------|-----------|--------|
| matrix | CPX / MATRIX / many pins | Finer grid, section layers, pin-access |
| power | POWER/GND class or names | Wide tracks / areas on plane layers |
| critical | critical / HS / clock / RF | Fine grid + vias |
| general | rest | Default free-angle |

Related: [ARCHITECTURE_ROUTER.md](ARCHITECTURE_ROUTER.md) · [CAPACITY_MESH.md](CAPACITY_MESH.md).

Boards mix dense multipin buses, power, and critical signals. physicsRouter
**auto-classifies nets** and routes each class with free-angle topological
search, painting a **shared obstacle map** so clearance, widths, and layer
policy stay consistent. Each bucket is attempted as an atomic native batch:
an incomplete multipin net is rolled back rather than leaking misleading
partial copper.

Halo-style concentric ring geometry has been **removed**; all strategies use
isotropic free-angle / native A\* with graph-seeded, capacity-negotiated
per-section layers and explicit offset vias.

## Strategies

| Strategy | Detection | Tuning |
|----------|-----------|--------|
| **matrix** | CPX-* / MATRIX* / ≥12 pins | Finer grid, section layer plan, reserved pin access |
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

Before bucket routing, `pin_access.py` enumerates legal surface-pad escape
sites using exact rotated/custom pad copper, all layers traversed by a through
via, hole spacing and Edge.Cuts. `global_router.py` then assigns each
hypergraph-tree edge a layer while negotiating coarse cell capacity. The C++
detailed router tries those finite access sites and section layers first; a
geometry fallback remains legal only if native DRC accepts the complete net.

HALO-90 v1.9.1 legally completes two of ten CPX nets; eight remain intentionally
open. Overall completion is 12/23. The first negotiation round finds 21
complete temporary candidates, but they have
more than 2,000 exact conflicts and are never exposed as legal copper. Three
bounded rounds reduce coarse overused cells from 2,825 to 1,163 before exact
legalization and targeted repair. This is the pre-v2 regression checkpoint.
Solving the rest still needs conflict-component route alternatives and
detouring global corridors; the new pin-access and section planner must be
measured in a fresh HALO run. The current legal selection has zero vias; the
earlier 42-via checkpoint contained
33 same-net or foreign via/pad violations under the corrected rule.

## Constraints

| Constraint | Source |
|------------|--------|
| Clearance | DesignRules / KiCad net classes |
| Track width | `track_width_for_net` (+ power boost) |
| Layers | DSATUR seed + capacity-negotiated per-tree-edge assignment |
| Priority | PlacementConfig weights |

## API

```python
from physics_router.hybrid_route import classify_board, hybrid_route

plan = classify_board(board, config, rules)
result = hybrid_route(board, config, rules)
# result.quality["hybrid_plan"]
# result.quality["graph_topology"]  # components, cycles, crossings, cuts
# result.quality["production_route_plan"]  # access sites + section layers
# result.quality["manufacturing_gate"]     # all nets + native DRC; KiCad pending
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
