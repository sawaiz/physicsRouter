# Hybrid multi-strategy routing

Boards are rarely one topology. A LED earring ring wants **concentric arcs**;
power wants **wide tracks on planes**; high-speed nets want **careful free-angle**
with vias. physicsRouter **auto-detects** which nets (and regions) fit which
algorithm, then routes them in phases on a **shared obstacle map** so spacing,
widths, and electrical policy stay consistent.

## Goals

1. **Auto-detect** region/net class → routing technique  
2. Allow **different algorithms per net / section**  
3. Preserve **design constraints**: clearance, track width, stackup layer
   preference, net weights / critical flags  

## Detection

| Signal | Strategy | Why |
|--------|----------|-----|
| CPX-* / pins on LED ring annulus | `ring` | halo.js concentric tracks |
| Power / GND (class or name) | `power` | wider copper, plane-prefer layers |
| Critical, HS, clock, RF, high weight | `critical` | free-angle + vias, finer grid |
| Everything else | `general` | isotropic free-angle / native |

Geometry:

- **Ring region** — LED footprints form a circle (see `detect_led_ring`)  
- **Core region** — pins near board/ring center (MCU, passives)  
- **Board** — mixed / default  

Classifier: `physics_router.hybrid_route.classify_board`.

## Paint order

```
ring  →  power  →  critical  →  general
```

Each phase:

1. Routes only its net subset  
2. Paints finished copper into the obstacle map (`seed_result`)  
3. Later phases must clear that copper (same clearance floors)  

Widths / clearances come from `DesignRules` + `PlacementConfig` labels
(`track_width_for_net`, `clearance_for_net`, `layers_for_net`, `weight_for_net`).

## API

```python
from physics_router.hybrid_route import classify_board, hybrid_route
from physics_router.design_rules import load_design_rules

plan = classify_board(board, config, rules)
print(plan.to_dict()["by_strategy"])

result = hybrid_route(board, config, rules, clearance_mm=0.15)
# result.quality["hybrid_plan"]  → full assignment dump
```

Entry points that use hybrid by default:

- `clearance_aware_route(..., style="auto"|"hybrid")`  
- `topor_style_route` / TopoR UI job when a ring or multi-class board is detected  
- Continuous **Improve** strategy `"hybrid"` first  

Force a single technique:

```python
clearance_aware_route(board, config, style="ring")       # polar only
clearance_aware_route(board, config, style="isotropic", skip_hybrid=True)
```

## Constraints contract

| Constraint | Source | Enforcement |
|------------|--------|-------------|
| Min clearance | KiCad DRC / DesignRules | ObstacleMap inflate + native/KiCad DRC |
| Track width | net class + POWER boost | `_net_width` / `track_width_for_net` |
| Layer preference | stackup + net class | `layers_for_net` per net |
| Net priority | PlacementConfig weights | net paint order within a phase |
| Copper legality | always-on DRC | repair + purge + optional kicad-cli |

Algorithms may differ; **illegal copper is never preferred over open edges**
(`soft_fallback=False`).

## Related

- [HALO_RING_ROUTING.md](HALO_RING_ROUTING.md) — polar track model  
- [ARCHITECTURE_ROUTER.md](ARCHITECTURE_ROUTER.md) — topology pipeline  
