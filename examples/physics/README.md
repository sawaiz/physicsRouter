# Physics / Muon3 example

Board from the sibling **`../physics`** project (Muon3 cosmic-ray telescope).

| Path | Role |
|------|------|
| `../physics/pcb/muon3.kicad_pcb` | Project shell — **empty** (no footprints) |
| `../physics/.../muon_telescope_v10/muon_telescope.kicad_pcb` | **Full board** used here (~111 comps, ~100 nets, 4-layer) |
| `placement_config.yaml` | Net weights, notes, functional regions, floorplan seeds |

## Board envelope

- **Generous outline:** 140 × 110 mm (source content ~108 × 86 plus margin)
- Functional floorplan applied via `fixed` placements + `regions`

| Region | Contents |
|--------|----------|
| `sipm_edge` | J2–J5 SiPM / panel connectors (left) |
| `afe_ch0`…`afe_ch3` | Per-channel TIA (U10x) + comparator (U1x2) + passives |
| `digital_core` | FPGA U3, flash U6, DAC U4, env U5, TCXO X1 |
| `power_hv` | VSYS, bucks U11/U12, LT3482 HV boost U10, L1/L2, D10 |
| `thermal_io` | NTC headers J9/J10 |
| `expansion_radio` | Debug / nRF / GNSS headers J6–J8 (right edge) |

## Net priorities (summary)

| Class | Examples | Weight (base) |
|-------|----------|----------------|
| Power / ground | GND, VSYS, +3V3, +1V2 | 5–6 |
| HV bias | BOOST_SW, HV_RAW, HV_SIPMn | 4.5–5 |
| AFE analog | SIPM_ANn, TIAn, INAn | 4.5–5 + EMI |
| Timing | CMPn, TCXO_CLK, GPS_PPS | 4–4.5 |
| Digital / I2C / SPI | SDA/SCL, CFG_*, NRF_* | 3–3.8 |
| Unconnected pads | `unconnected-(…)` | 0.1 |

Notes and power-loop groups follow `../physics/pcb/DESIGN_RULES.md` and `PART_SELECTION.md`.

## Serve

```bash
PHYSICS_ROUTER_PRESET=physics physics-router serve --host 127.0.0.1 --port 8765
# → http://127.0.0.1:8765/
# Preset: “Muon3 / physics” — 2D floorplan + auto GLB export
```

Hard-refresh the browser (or re-click **Load preset**) if the 3D model was still showing HALO-90; assets are cache-busted per file mtime and scoped to the active preset.
