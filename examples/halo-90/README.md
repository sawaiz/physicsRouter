# HALO-90 test project

Open-source LED earring from [openKolibri/halo-90](https://github.com/openKolibri/halo-90). Used as a real-world KiCad stress test for physicsRouter (dense circular board, charlieplex matrix, coin-cell power).

## Setup

```bash
# From physicsRouter repo root
git clone git@github.com:openKolibri/halo-90.git third_party/halo-90
```

KiCad sources: `third_party/halo-90/pcb/halo-90.kicad_{pcb,sch,pro}`

## Label file

`placement_config.yaml` is **hand-authored** from:

| Source | What we used |
|--------|----------------|
| [halo-90 readme](https://github.com/openKolibri/halo-90) | Architecture, pin table, power modes, mechanical dims |
| Schematic / PCB nets | Exact net names and connectivity |
| Component PDFs under `pcb/components/` | STM8L, LED Vf, mic, IMU I2C, battery holder |

### Net priority summary

| Nets | Weight | Why |
|------|--------|-----|
| `+3V`, `GND` | 5.0 | CR2032 rail; LED drive limited by ESR/GPIO; decoupling loop |
| `CPX-0`…`CPX-9` | 4.0 | Charlieplex @ >1 kHz, high di/dt, no series R, EMI |
| `MIC` | 3.5 | ADC audio path — keep quiet vs CPX |
| `SDA`/`SCL` | 3.0 | I2C pair + 10k pull-ups (IMU optional) |
| `Net-(R3-Pad2)` (NRST) | 3.0 | Reset + 10k pull-up + pogo RST |
| `SW-A`/`SW-B` | 2.5 | Mode / deep-sleep button |
| `Net-(TP2-Pad1)` (SWIM) | 2.5 | Factory programming |
| `TX`/`RX`, `XL-INT*` | 2.0 | Debug / optional IMU IRQs |

### Fixed geometry

MCU, battery, hook, mic, button, passives, pogo pads, and tooling holes are **locked** to the released layout (origin = board center). The 90 LEDs stay on the product ring (~11 mm radius, 4°).

### Product constraints (from readme)

- Diameter **24 mm**, eyelet +2 mm → ~24×26 mm bounding box  
- Mass ~5.2 g with cell  
- Abs max battery **3.6 V**; operating **1.8–3.6 V**  
- Power draw ~15 µA sleep … ~25 mA max; modes ~2–12 mA  
- LEDs: 0402 red, Vf 2.0–2.6 V, cathodes toward center  

## KiCad stackup (from board)

HALO-90 is a **4-layer** design in KiCad:

`F.Cu` → dielectric → `In1.Cu` → dielectric → `In2.Cu` → dielectric → `B.Cu`  
(≈0.035 mm Cu, FR4 εᵣ≈4.5, overall thickness ≈1 mm in the project.)

DRC floors from `halo-90.kicad_pro` (Default net class): clearance/track ≈ **0.127 mm**, via ≈ **0.45 / 0.2 mm**.

```bash
physics-router rules --pcb third_party/halo-90/pcb/halo-90.kicad_pcb
physics-router pre-route --config examples/halo-90/placement_config.yaml \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb
```

## Run

```bash
source .venv/bin/activate

# Score released placement
physics-router score \
  --config examples/halo-90/placement_config.yaml \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb

# Place (most parts locked — validates scoring / SA on free parts if any)
physics-router place \
  --config examples/halo-90/placement_config.yaml \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb \
  --candidates 2 --iterations 200 \
  --out-json examples/halo-90/placement_result.json

# Route (build the native module first; there is no Python geometry fallback)
bash scripts/build_native.sh
physics-router route \
  --config examples/halo-90/placement_config.yaml \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb \
  --clearance 0.15 --grid 0.5 \
  --out-json examples/halo-90/route_result.json

# OpenEMS geometry export
physics-router export-openems \
  --config examples/halo-90/placement_config.yaml \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb \
  --out-dir examples/halo-90/openems_export
```

## Current native-router snapshot

The v1.7 documentation run uses the board-derived rules, atomic multipin
transactions, oriented pad/layer-aware obstacles, width-aware seed inflation,
topology-safe polish, parallel bucket orders, and a native organic GND area
with a track/via backbone. It commits **9/23 nets**, 144 segments (265.1 mm),
21 vias, and one area in about 3.2 seconds. Exact native track/via DRC reports
zero shorts, spacing violations, and Edge.Cuts escapes. The completed CPX nets
are `CPX-1` and `CPX-5`; 14 nets remain honestly open.

This lower number is intentional and supersedes the old 17/23 snapshot. That
result counted inner-layer segments as connected at front-only SMD pads even
when no via existed. v1.7 checks every anchor's exposed copper layers and uses
explicit two-via escape geometry when an inner-layer corridor is required.

This is a legal partial route, not fabrication sign-off. Refill the exported
zones and run KiCad DRC to validate the final filled polygons and thermals.
The machine-readable result is
[`../../docs/images/routing_process/drc_report.json`](../../docs/images/routing_process/drc_report.json).
The slower 19/23 Python-orchestrated experiment is preserved separately in
[`python_multipin_experiment.json`](python_multipin_experiment.json).

## Checked-in results & figures

Regenerate the current process and DRC figures with:

```bash
bash scripts/build_native.sh
PYTHONPATH=native/build:src python scripts/render_routing_process.py --halo
```

This needs `third_party/halo-90` and matplotlib.

| File | Description |
|------|-------------|
| `benchmark_results.json` | Timings + score + route metrics |
| `route_guide.json` | Free-angle guide segments |
| `route_result.json` | Clearance multilayer route (1 mm grid) |
| `../../docs/images/placement_overview.png` | Footprint map |
| `../../docs/images/route_guide.png` | Guide routing viz |
| `../../docs/images/route_by_layer.png` | Per-layer route viz |
| `../../docs/images/score_breakdown.png` | Cost bar chart |
| `../../docs/images/runtimes.png` | Timing bar chart |

### Legacy benchmark snapshot

| Step | Time | Notes |
|------|------|--------|
| score | ~0.04 s | ngspice + EMI proxies |
| route-guide | ~11 s | 207 segs, 854 mm |
| route (grid 1 mm) | ~35 s | 208 segs, pre-v1.5 route policy |

## KiCad DRC + official renders

```bash
physics-router drc --pcb third_party/halo-90/pcb/halo-90.kicad_pcb \
  --out-dir examples/halo-90/kicad_validation/drc
physics-router render --pcb third_party/halo-90/pcb/halo-90.kicad_pcb \
  --out-dir examples/halo-90/kicad_validation/renders
python scripts/generate_kicad_renders.py
```

| Artifact | Path |
|----------|------|
| DRC summary | `kicad_validation/drc_summary.json` |
| Layer SVGs (cli) | `kicad_validation/renders/svg_cli/` |
| Layer SVGs (pcbnew) | `kicad_validation/renders/svg_pcbnew/` |
| 3D PNGs | `docs/images/kicad/kicad_3d_*.png` |

## Licence

HALO-90 content remains under its upstream licence (see `third_party/halo-90`). This folder only adds physicsRouter labels and scripts.
