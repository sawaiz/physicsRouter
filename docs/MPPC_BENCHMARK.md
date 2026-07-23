# Benchmark: mppcInterface v1.3 — human vs topological autorouter

**Primary HEP golden for physicsRouter.**  
Source: [muonTelescope/mppcInterface](https://github.com/muonTelescope/mppcInterface) · commit **`580c61d`** (*Initial update to 1.3*, 2020-08-21).

SiPM/MPPC 8-channel readout for a muon telescope (bias + analog FE + FPGA coincidence + Pi host). Upstream design notes cite **sPHENIX-class** bias/coincidence topologies.

---

## Why this revision (not HEAD)

| Rev | Stack | Human unrouted | Role |
|-----|-------|----------------:|------|
| **`580c61d` (pinned)** | **4-layer** F/In1/In2/B | **0** | **Complete human golden** |
| HEAD `4971c45` | 2-layer | 9 | Current gateware tree; weaker oracle |
| `a98f88b` (1v2) | 2-layer | many | Densest copper, incomplete nets |

Pinned files: `examples/mppc-interface/mppcInterface_v1.3.kicad_pcb` (+ `.kicad_pro`).

---

## Board facts (human golden)

| Item | Value |
|------|------:|
| Outline | **65 × 30 mm** |
| Components | **161** |
| Nets | **85** (all with copper) |
| Layers | F.Cu · In1.Cu · In2.Cu · B.Cu |
| Segments | **1199** |
| Vias | **155** |
| Areas / pours | **61** |
| Track length | **1931.8 mm** |
| Topology guide length | ~1563 mm |
| Steiner multipin nets | 60 |
| Cut preflight | feasible (no saturated cuts @ 0.3 mm pitch) |

---

## Human vs autorouter

![compare](images/golden/mppc_v13_compare.png)

![metrics](images/golden/mppc_v13_metrics.png)

![human layers](images/golden/mppc_v13_human_layers.png)

### Scorecard (policy: open > short)

| Metric | Human | Autorouter |
|--------|------:|-----------:|
| Electrical completeness | **85/85 nets** | see `viewer/runs/mppc_v1.3/benchmark_row.json` |
| Hard DRC | fab reference | must stay **0** on committed copper |
| Length / vias / segs | 1932 mm · 155 · 1199 | AR row in metrics chart |
| Pours | 61 areas | AR under-uses pours vs human |

**Reading the result**

- **Grade A / completion 1.0** → manufacturing-gate style success vs this golden.  
- **Completion &lt; 1 and hard_drc = 0** → honest partial (preferred over shorts).  
- **Human 4L pours** are a return-path asset; AR must grow pours for power/GND after full legal routes (`improve --physics-feedback`).

Reproduce:

```bash
bash scripts/build_native.sh
python scripts/run_mppc_benchmark.py
# or
physics-router route \
  --pcb examples/mppc-interface/mppcInterface_v1.3.kicad_pcb \
  --config examples/mppc-interface/placement_config.yaml \
  --pipeline capacity --effort 0.5 \
  --out-json /tmp/mppc_ar.json --out-pcb /tmp/mppc_ar.kicad_pcb
```

---

## Why it defines project scope

1. **Real instrument board**, not a synthetic cross.  
2. **Complete multilayer human route** — fair score vs autorouter.  
3. Stresses **HV · analog · digital · power** together.  
4. Matches the stack we optimize: **topology (Steiner/capacity) → free-angle geometry → 0 hard DRC**.

Related galleries: [examples/golden/RESULTS.md](../examples/golden/RESULTS.md) (CERN-OHL suite) · [GOLDEN_CORPUS.md](GOLDEN_CORPUS.md).
