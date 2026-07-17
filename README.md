# Physics-Aware KiCad Placement & Routing Engine

A **KiCad placement and routing engine** that combines topological (TopoR-style) layout with **physics simulations** (Ngspice, OpenEMS) so boards are not only fully routed, but also validated for real-world electrical behavior.

Inspired by [TopoR](https://en.wikipedia.org/wiki/TopoR) (*Topological Router*) — gridless routing, no preferred directions, free wire angles.

**Assumption for placement quality:** projects provide **full KiCad files** plus **well-labeled nets** with **weights** and **notes** (see `placement_config.yaml`). Physics sims rank the best geometric candidates.

## Quick start

```bash
# Install (editable)
python3 -m pip install -e ".[dev]"

# Write an example labeled-net config
physics-router init-config -o examples/placement_config.yaml

# Multi-candidate placement (synthetic demo board if no .kicad_pcb)
physics-router place \
  --config examples/placement_config.yaml \
  --out-json placement_result.json

# With a real board
physics-router place \
  --config placement_config.yaml \
  --pcb path/to/board.kicad_pcb \
  --out-pcb path/to/board_placed.kicad_pcb \
  --out-json placement_result.json

# Score current layout only
physics-router score --config examples/placement_config.yaml

# Free-angle topological guide routes
physics-router route-guide --config examples/placement_config.yaml --out-json route_guide.json
```

## Labeled nets, weights, and notes

Placement is driven by a YAML/JSON config next to the KiCad project:

```yaml
nets:
  - name: SW
    net_class: power          # power | ground | clock | differential | analog | rf | ...
    weight: 5.0               # higher → pull connected parts closer
    critical: true
    power_loop_group: buck1   # minimize loop area for this group
    emi_sensitive: true
    simulate_spice: true      # include in Ngspice/proxy ranking
    simulate_em: true         # include in OpenEMS/proxy ranking
    max_length_mm: 12.0
    notes: "Switcher node — place L and FET tight."

fixed:
  - ref: J1
    x_mm: 2.0
    y_mm: 20.0
    locked: true
    notes: "USB on edge"

regions:
  - name: power
    x_min_mm: 0
    y_min_mm: 0
    x_max_mm: 25
    y_max_mm: 40
    preferred_refs: [U1, L1, C_IN]
```

Full example: [`examples/placement_config.yaml`](examples/placement_config.yaml).

## How placement chooses “best”

```
KiCad PCB + placement_config.yaml (labels/weights/notes)
        │
        ▼
┌─────────────────────────────┐
│ Seed N candidates           │  region-aware random + fixed parts
│ Simulated annealing         │  geometric multi-objective cost
└─────────────┬───────────────┘
              ▼
┌─────────────────────────────┐
│ Rank by geometry            │  wirelength × weights, loop area,
│                             │  critical nets, overlap, density,
│                             │  thermal, EMI proxy
└─────────────┬───────────────┘
              ▼
┌─────────────────────────────┐
│ Physics on top-N            │  Ngspice (or RL proxy) + OpenEMS
│                             │  (or EM proxy) re-score finalists
└─────────────┬───────────────┘
              ▼
   Best candidate → .kicad_pcb + JSON report
```

### Cost terms (lower is better)

| Term | Role |
|------|------|
| Weighted wirelength | HPWL × net `weight` / class boosts |
| Power loop area | BBox area of `power_loop_group` parts (EMI / L) |
| Critical net length | Extra cost for `critical` / `max_length_mm` nets |
| Overlap / regions | Legal packing + floorplan preferences |
| Density / thermal | Congestion and hot-part separation |
| EMI proxy | Sensitive nets away from analog; length |
| Spice score | Rail/loop proxy; real **ngspice** when installed |
| OpenEMS score | EMI proxy; **openEMS** binary detected when present |

Physics weights live under `physics:` in the config so you can emphasize loop inductance vs wirelength, etc.

## Architecture

| Module | Role |
|--------|------|
| `physics_router.models` | Net labels, regions, scores |
| `physics_router.config_io` | YAML/JSON load/save |
| `physics_router.kicad_io` | Read/write `.kicad_pcb` placements |
| `physics_router.physics` | Cost terms + Ngspice/OpenEMS backends |
| `physics_router.placement` | Multi-candidate SA + physics ranking |
| `physics_router.router` | TopoR-style free-angle **guide** routes |
| `physics_router.cli` | `physics-router` CLI |

## Topological routing (TopoR-inspired)

Guide routing (`route-guide`) builds free-angle polylines (no 45°/90° constraint) as a rubberband precursor. Full clearance-aware TopoR engine is next; placement already optimizes for fewer vias / shorter critical nets via physics scores.

## Setup

- Python 3.10+
- KiCad 8+ (for real projects; optional for synthetic demo)
- Ngspice (optional; improves spice ranking when on `PATH`)
- OpenEMS (optional; detected for EM path)

```bash
python3 -m pip install -e ".[dev]"
pytest
```

## Training data

See **[DATASETS.md](DATASETS.md)** for PCB corpora (PCBench, Open Schematics, Gerbers, conversion paths).

## References

- [TopoR (Wikipedia)](https://en.wikipedia.org/wiki/TopoR)
- Tal Dayan, *Rubberband based topological router* (PhD thesis, 1997)
- KiCad — target EDA host
