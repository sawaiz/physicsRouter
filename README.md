# Physics-Aware KiCad Placement & Routing Engine

A **KiCad placement and routing engine** that combines topological (TopoR-style) layout with **physics simulations** (Ngspice, OpenEMS) so boards are not only fully routed, but also validated for real-world electrical behavior.

Inspired by [TopoR](https://en.wikipedia.org/wiki/TopoR) — gridless free-angle routing, no preferred directions, clearance-aware multi-layer paths with vias.

**Inputs that unlock best results:** full KiCad projects (`.kicad_pcb` / `.kicad_sch`) plus **well-labeled nets** with **weights** and **notes**. Labels can be authored in YAML or **imported** from KiCad netclasses and schematic fields.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Example labeled-net config
physics-router init-config -o examples/placement_config.yaml

# Import labels from KiCad netclasses + schematics
physics-router import-nets \
  --pcb path/to/board.kicad_pcb \
  --project-dir path/to/project \
  -o placement_config.yaml

# Multi-candidate physics-aware placement
physics-router place \
  --config placement_config.yaml \
  --pcb path/to/board.kicad_pcb \
  --out-pcb board_placed.kicad_pcb \
  --out-json placement_result.json

# Clearance-aware TopoR free-angle routing
physics-router route \
  --config placement_config.yaml \
  --pcb board_placed.kicad_pcb \
  --clearance 0.2 \
  --out-json route_result.json \
  --out-pcb board_routed.kicad_pcb

# OpenEMS mesh export (placement + routes and/or Gerbers)
physics-router export-openems \
  --config placement_config.yaml \
  --pcb board_routed.kicad_pcb \
  --gerber F.Cu=fab/F_Cu.gbr \
  --gerber B.Cu=fab/B_Cu.gbr \
  --out-dir openems_export
```

Synthetic demo (no PCB file):

```bash
physics-router place --config examples/placement_config.yaml
physics-router route --config examples/placement_config.yaml --clearance 0.2
physics-router export-openems --config examples/placement_config.yaml --out-dir openems_export
```

## Import nets from KiCad

`import-nets` builds `NetLabel` entries from:

| Source | What is read |
|--------|----------------|
| `.kicad_pcb` `(net_class …)` / `(add_net …)` | Class name, clearance, track width, class notes |
| `.kicad_pcb` `(net id "name")` | Net inventory |
| `.kicad_sch` labels / global / hierarchical | Net names + shapes / properties |
| Schematic text `NET: note…` | Designer notes attached to nets |

Heuristics map names/classes → `power` / `ground` / `clock` / `differential` / … with default **weights**, **critical** flags, `simulate_spice` / `simulate_em`, and auto **power_loop_group** for switcher nets.

```bash
physics-router import-nets --pcb board.kicad_pcb --sch root.kicad_sch -o placement_config.yaml
# or scan all schematics:
physics-router import-nets --pcb board.kicad_pcb --project-dir . -o placement_config.yaml --override
```

## Labeled nets (YAML)

```yaml
nets:
  - name: SW
    net_class: power
    weight: 5.0
    critical: true
    power_loop_group: buck1
    emi_sensitive: true
    simulate_spice: true
    simulate_em: true
    notes: "Switcher node — place L and FET tight."
```

Full example: [`examples/placement_config.yaml`](examples/placement_config.yaml).

## Placement (physics-ranked multi-candidate)

```
KiCad PCB + labeled nets
  → seed N region-aware candidates
  → simulated annealing (weighted WL, loop area, critical nets, overlap, density, thermal, EMI)
  → Ngspice / OpenEMS proxies on every candidate
  → best → .kicad_pcb + JSON
```

## Clearance-aware TopoR routing

| Feature | Behavior |
|---------|----------|
| Free angles | 16-direction steps + line-of-sight rubberband (not only 45°/90°) |
| Clearance | Obstacle map from courtyards/pads inflated by clearance; same-net may pass |
| Priority | High-weight / critical nets routed first |
| Multi-layer | Alternate layer + vias when same-layer path blocked |
| Copper paint | Routed traces become obstacles for later nets |
| Output | JSON + optional `(segment)` / `(via)` append to `.kicad_pcb` |

```bash
physics-router route --config placement_config.yaml --pcb board.kicad_pcb \
  --clearance 0.2 --out-json route_result.json --out-pcb board_routed.kicad_pcb
```

## OpenEMS export

Builds a simulation bundle from **placement**, **routes**, and/or **Gerbers**:

| Artifact | Content |
|----------|---------|
| `board_geometry.json` | Stackup + copper boxes/polylines (mm) |
| `simulate_board.py` | CSXCAD/openEMS driver loading the JSON |
| `OPENEMS_README.txt` | How to run |

Gerber path: minimal RS-274X parser (apertures, draws, flashes).

```bash
physics-router export-openems \
  --config placement_config.yaml \
  --pcb board.kicad_pcb \
  --gerber F.Cu=F_Cu.gbr \
  --out-dir openems_export
# then: python openems_export/simulate_board.py   # needs openEMS + CSXCAD
```

Placement still uses a **fast EMI proxy** in-loop; use export for high-fidelity FDTD on shortlists.

## Architecture

| Module | Role |
|--------|------|
| `models` | Net labels, regions, scores |
| `config_io` | YAML/JSON config |
| `net_import` | KiCad netclass + schematic → labels |
| `kicad_io` | Read/write footprint positions |
| `physics` | Multi-objective cost + Ngspice/OpenEMS backends |
| `placement` | Multi-candidate SA + ranking |
| `router` | Clearance-aware free-angle TopoR router |
| `openems_export` | Mesh/geometry + Gerber → openEMS |
| `cli` | `physics-router` entry point |

## Setup

- Python 3.10+
- KiCad 8+ (real projects)
- Ngspice (optional, improves spice ranking)
- OpenEMS + CSXCAD (optional, for `simulate_board.py`)

```bash
pip install -e ".[dev]"
pytest
```

## Training data

See **[DATASETS.md](DATASETS.md)** for PCB corpora and conversion paths.

## References

- [TopoR (Wikipedia)](https://en.wikipedia.org/wiki/TopoR)
- Tal Dayan, *Rubberband based topological router* (1997)
- [openEMS](https://docs.openems.de/)
- KiCad — target EDA host
