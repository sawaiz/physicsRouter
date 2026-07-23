# Benchmark: mppcInterface v1.3 (human vs topological autorouter)

**Primary golden board for physicsRouter.** HEP SiPM/MPPC readout from
[muonTelescope/mppcInterface](https://github.com/muonTelescope/mppcInterface)
commit **`580c61d`** (*Initial update to 1.3*, 2020-08-21).

Design lineage includes sPHENIX-class bias/coincidence ideas (see upstream readme).
This revision is the best **electrically complete** human route in the repo history
(0 nets without copper; 4-layer stack; pours present).

---

## Board facts

| Item | Value |
|------|-------|
| Outline | **65.0 × 30.0 mm** |
| Components | **161** |
| Nets | **85** |
| Copper layers | `F.Cu, In1.Cu, In2.Cu, B.Cu` |
| Human segments | **1199** |
| Human vias | **155** |
| Human areas (pours) | **61** |
| Human length | **1931.8 mm** |
| Human unrouted | **0** |
| Topology guide length | 1562.555 mm |
| Steiner multipin nets | 60 |
| Cut preflight feasible | True |

Pinned files: `examples/mppc-interface/mppcInterface_v1.3.kicad_pcb` (+ `.kicad_pro`).

---

## Human vs autorouter

![compare](images/golden/mppc_v13_compare.png)

![metrics](images/golden/mppc_v13_metrics.png)

![human layers](images/golden/mppc_v13_human_layers.png)

## Score vs human copper

| Metric | Human | Autorouter |
|--------|------:|-----------:|
| Status | golden | **honest partial** (passed open>short gate) |
| Golden grade | — | **F** |
| Golden score | — | **18.24** |
| Completion vs human nets | 100% | **48.2%** (41/85) |
| Hard DRC | 0 (assumed fabbed) | **0** |
| Length (mm) | 1931.8 | 805.1 |
| Vias | 155 | 94 |
| Segments | 1199 | 318 |
| Areas/pours | 61 | 3 |
| Wall time (s) | — | 1504 (~28 min) |
| Pipeline | hand | capacity · effort 0.45 · no hard deadline · CBS off · dense-board hybrid caps |

Missing nets vs human (44): `+3V3, +5V, CH0, CH1, CH2, CH3, CH4, CH5, CH6, CH7, CLK, DAC0, DAC1, DAC3, DAC4, DAC6, DAC7, FPGA_{DONE}, GND, GPIO17, GPIO18, GPIO22, GPIO23, GPIO27`

### Policy reading

- **Completion < 1 with hard_drc = 0** is an *honest partial*: open copper beat shorts.
- Length shorter than human is only “better” if completion ≈ 1.0.
- Human 4-layer pours (61 areas) are a return-path asset the AR still under-uses.

### Why grade F (and how to raise it)

Full post-mortem: **[ROUTING_DIFFICULTIES.md](ROUTING_DIFFICULTIES.md)** ·
snapshot log **[mppc_v13_route_diagnostics.md](mppc_v13_route_diagnostics.md)**.

| Issue | Evidence on this board |
|-------|------------------------|
| Open multipin | 44 nets: GND/+5V/+3V3/HV, all CH*, most DAC*, SPI, GPIO |
| Pours missing | Human 61 areas vs AR 3 |
| Corridor hogging | `+5V-A` 302 mm vs human 85 mm; seals space for digital +5V |
| Empty rip-up | 23 `ripup(empty)` attempts (GPIO/FPGA/DAC) |
| Global overflow | final_overflow≈51, mesh_overflow_nodes≈3206 |
| Config gap (fixed) | `+5V` was not listed → weight 1.0; now weighted in placement_config |

Future runs auto-write `viewer/runs/mppc_v1.3/route_diagnostics.{json,md}` via
`route_diagnostics.py` / golden-eval.

---

## Why this board for topological autorouting

1. **Real HEP instrument** (SiPM bias, analog front-end, FPGA coincidence, Pi host).
2. **Complete human multilayer golden** at `580c61d` (HEAD is a later 2L/lib revision with open nets).
3. Stresses **power + HV + analog + digital** together — not a toy cross-over.
4. Fits the project scope: **topology (Steiner/capacity) → free-angle geometry → 0 hard DRC**.

### History note

Earlier commits (`8aa2399`→`a98f88b`) show progressive 2-layer routing; v1.3 is the
clean multilayer snapshot. See git log on `muonTelescope/mppcInterface`.

---

## Reproduce

```bash
bash scripts/build_native.sh
# PCB already pinned under examples/mppc-interface/
python scripts/run_mppc_benchmark.py

physics-router golden-eval \
  --id mppc_v1.3 \
  --manifest examples/mppc-interface/manifest.yaml \
  --pipeline capacity --effort 0.5
```

Artifacts: `viewer/runs/mppc_v1.3/` · images: `docs/images/golden/mppc_v13_*.png`.

_Generated 2026-07-23 · physicsRouter topological autorouter._

