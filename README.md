# Physics-Aware KiCad Placement & Routing Engine

A **KiCad placement and routing engine** that combines topological (TopoR-style) layout with physical simulations (Ngspice, OpenEMS) so boards are not only fully routed, but also validated for real-world electrical behavior.

Inspired by [TopoR](https://en.wikipedia.org/wiki/TopoR) (*Topological Router*) — a gridless autorouter with no preferred routing directions, free wire angles, and automatic length/shape optimization when components move.

## Overview

Standard autorouters force 45°/90° channels and ignore physics. This project aims to produce production-ready PCB layouts by:

1. **Placing and routing topologically** (TopoR-like): flexible paths, efficient space use, fewer vias, lower crosstalk risk from orthogonal preferred-direction layers.
2. **Validating with physics** during the layout loop — not only after DRC.

Physical concerns standard routers ignore:

- **EMI emissions** — OpenEMS to simulate and minimize interference
- **Power loops** — minimize loop area and parasitic inductance
- **Signal integrity** — impedance, length, and coupling constraints
- **Circuit behavior** — Ngspice for net-level checks against design intent

## Topological routing (TopoR-inspired)

[TopoR](https://en.wikipedia.org/wiki/TopoR) is a topological PCB autorouter (Eremex) known for:

| Idea | What it means for this engine |
|------|-------------------------------|
| No preferred layer directions | Traces are not forced into H/V “channels”; routing is freer and denser |
| Angles not limited to 45°/90° | Wires can use arbitrary angles (and optionally arcs) |
| Gridless topology | Clearances and topology drive the shape, not a fixed grid |
| Auto-optimize on move | When parts or vias move, wire length/shape re-optimizes with clearance |
| Multi-variant search | Keep several layout candidates; drop the worst by length / via count |
| Placement assist | Automatic component placement for the whole board or a region (seed for manual work) |
| Necking & teardrops | Trace width reduction in tight spots; smooth pad entry |
| BGA-aware strategies | Special handling to cut vias, density, and sometimes layer count |
| Single-layer modes | Algorithms that minimize or eliminate interlayer junctions |

Related open-source lineage: gEDA’s **Toporouter** (rubberband / topological methods; also adapted toward KiCad). This project targets **first-class KiCad placement + routing**, with physics in the loop.

## Features (planned)

- **KiCad-native placement & routing engine** — schematics and PCB as source of truth
- **TopoR-style topological autoroute** — gridless, free angles, no preferred directions
- **Component placement** — global and region-based auto-placement
- **Physics-in-the-loop** — iterative refine using Ngspice / OpenEMS feedback
- **Production focus** — fewer manufacturing and EMI surprises after layout

## Architecture (high level)

```
KiCad schematic / PCB
        │
        ▼
┌───────────────────┐
│ Placement engine  │  ← topological / constraint-aware placement
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ Topological router│  ← TopoR-inspired free-angle routing
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ Physics validators│  ← Ngspice, OpenEMS, SI / EMI / power-loop metrics
└─────────┬─────────┘
          ▼
   Accept / re-place / re-route
          │
          ▼
   Updated KiCad PCB
```

## Training data

See **[DATASETS.md](DATASETS.md)** for PCB placement/routing training sources:

- Native KiCad corpora (PCBench, Open Schematics, demos, GitHub crawls)
- Conversion paths (EAGLE, EasyEDA/OSHWLab, Altium → KiCad; Specctra DSN/SES)
- Gerber collections for routed copper / physics modalities
- Recommended corpus layout and labeling strategy

## Setup requirements (planned)

- KiCad 8+
- Ngspice
- OpenEMS
- Python 3.10+

## References

- [TopoR (Wikipedia)](https://en.wikipedia.org/wiki/TopoR) — topological router concepts and history
- Tal Dayan, *Rubberband based topological router* (PhD thesis, 1997) — algorithms behind open-source Toporouter-style tools
- KiCad — open-source EDA; target integration host for this engine
