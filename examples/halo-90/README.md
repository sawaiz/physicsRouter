# HALO-90 example

**TL;DR:** Dense 24 mm LED earring board — our main **stress test**. Zero-violation policy often leaves nets open rather than shorted. Config: `placement_config.yaml`. PCB: `third_party/halo-90/pcb/`.

Upstream: [openKolibri/halo-90](https://github.com/openKolibri/halo-90).

---

## Setup

```bash
# from physicsRouter root
git clone git@github.com:openKolibri/halo-90.git third_party/halo-90
# or https://github.com/openKolibri/halo-90.git
```

Sources: `third_party/halo-90/pcb/halo-90.kicad_{pcb,sch,pro}`

---

## Run

```bash
# Live native progress window
physics-router route \
  --config examples/halo-90/placement_config.yaml \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb \
  --pipeline capacity --effort 0.55 \
  --out-json examples/halo-90/route_result.json \
  --out-pcb /tmp/halo_routed.kicad_pcb

# Headless (dense — allow partial for a quick smoke)
physics-router smoke \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb \
  --config examples/halo-90/placement_config.yaml \
  --min-grade F --no-fail-on-drc

# Explicit headless route
physics-router route --no-ui \
  --config examples/halo-90/placement_config.yaml \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb \
  --pipeline capacity --effort 0.55 \
  --out-json examples/halo-90/route_result.json \
  --out-pcb /tmp/halo_routed.kicad_pcb
```

---

## Board facts

| Item | Value |
|------|--------|
| Size | ~24 mm diameter (ring Edge.Cuts) |
| Stackup | 4-layer F / In1 / In2 / B |
| Stress | 90 LEDs charlieplex `CPX-0`…, tight free-angle |
| DRC floors (Default) | ~0.127 mm clearance/track; via ~0.45/0.2 mm |

```bash
physics-router rules --pcb third_party/halo-90/pcb/halo-90.kicad_pcb
physics-router pre-route --config examples/halo-90/placement_config.yaml \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb
```

---

## Config notes

`placement_config.yaml` is hand-tuned from schematic + product readme:

| Nets | Weight | Why |
|------|-------:|-----|
| `+3V`, `GND` | 5 | Power / battery |
| `CPX-0`…`CPX-9` | 4 | Charlieplex matrix |
| `MIC`, I2C, reset | 2.5–3.5 | Sensitive / critical |
| Switches / debug | 2–2.5 | Secondary |

LEDs locked via `lock_ref_prefixes: ["D"]`. MCU, battery, mechanicals fixed.

---

## Artifacts in this folder

| File | Content |
|------|---------|
| `placement_config.yaml` | Labels / locks |
| `route_result.json` / `route_guide.json` | Example route dumps |
| `benchmark_results.json` | Bench numbers |
| `kicad_validation/` | DRC + SVG renders |
| `zero_violation_*.json` | Zero-violation experiment logs |

History of failures / fixes: [../../docs/AUTOROUTER_FAILURE_ANALYSIS.md](../../docs/AUTOROUTER_FAILURE_ANALYSIS.md).

---

## Doc map

[../../docs/README.md](../../docs/README.md) · [../../docs/USER_GUIDE.md](../../docs/USER_GUIDE.md) · [../../docs/BENCHMARKS.md](../../docs/BENCHMARKS.md)
