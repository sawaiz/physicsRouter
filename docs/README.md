# Documentation index

**Start here if you are new:** [QUICKSTART.md](QUICKSTART.md) → [USER_GUIDE.md](USER_GUIDE.md)

Everything else is optional depth. Each page starts with a **TL;DR** so you can skim.

---

## By task

| I want to… | Go to |
|------------|--------|
| **See the flagship human vs AR benchmark** | [MPPC_BENCHMARK.md](MPPC_BENCHMARK.md) |
| Install and route a board in 5 minutes | [QUICKSTART.md](QUICKSTART.md) |
| Use the web UI, lock nets, keep-outs, plugin | [USER_GUIDE.md](USER_GUIDE.md) |
| See every CLI flag | [CLI.md](CLI.md) |
| Understand why open > short | [../DESIGN.md](../DESIGN.md) |
| Understand capacity mesh / pipeline stages | [CAPACITY_MESH.md](CAPACITY_MESH.md) |
| Build / debug the C++ core | [../native/README.md](../native/README.md) |
| Install the KiCad plugin | [../kicad_plugins/README.md](../kicad_plugins/README.md) |
| Run HALO-90 / Muon3 examples | [../examples/halo-90/README.md](../examples/halo-90/README.md) · [../examples/physics/README.md](../examples/physics/README.md) |
| Read speed numbers / images | [BENCHMARKS.md](BENCHMARKS.md) |
| FreeRouting DSN/SES | [CLI.md](CLI.md#freerouting) · [../examples/demo/FREEROUTING.md](../examples/demo/FREEROUTING.md) |
| Golden boards vs human copper | [CLI.md](CLI.md#golden) · [GOLDEN_CORPUS.md](GOLDEN_CORPUS.md) · [../examples/golden/README.md](../examples/golden/README.md) |
| JLCPCB fab profiles | [JLCPCB_4LAYER.md](JLCPCB_4LAYER.md) |
| Why dense boards fail historically | [AUTOROUTER_FAILURE_ANALYSIS.md](AUTOROUTER_FAILURE_ANALYSIS.md) |
| Topology / graph theory plane | [ARCHITECTURE_ROUTER.md](ARCHITECTURE_ROUTER.md) |
| Hybrid strategy buckets | [HYBRID_ROUTING.md](HYBRID_ROUTING.md) |
| TopoR product inspiration | [TOPOR.md](TOPOR.md) |
| Papers & bibliography | [../RESEARCH.md](../RESEARCH.md) |
| Datasets / board inventory | [../DATASETS.md](../DATASETS.md) |

---

## Mental model (one paragraph)

Python loads a KiCad board, imports nets, and plans **pin-access** + a **capacity mesh**. The **C++** core then draws free-angle copper with exact clearance, committing **whole nets only**. Failed nets stay empty. You write a `.kicad_pcb`, then trust **KiCad DRC** as the oracle.

```text
.kicad_pcb ──► model ──► plan ──► pr_native copper ──► DRC ──► artifacts
                 ▲                    │
                 └── policy / UI / CLI ┘
```

---

## Image galleries

| Gallery | Content |
|---------|---------|
| [images/routing_process/](images/routing_process/README.md) | Stage-by-stage synthetic + HALO renders |
| [images/kicad/](images/kicad/README.md) | Official KiCad plots / 3D |
| [images/topor/](images/topor/README.md) | TopoR reference screenshots |

---

## Suggested reading order

1. [QUICKSTART.md](QUICKSTART.md) — hands on  
2. [USER_GUIDE.md](USER_GUIDE.md) — day-to-day  
3. [../DESIGN.md](../DESIGN.md) — principles  
4. [CAPACITY_MESH.md](CAPACITY_MESH.md) + [ARCHITECTURE_ROUTER.md](ARCHITECTURE_ROUTER.md) — how routing works  
5. [AUTOROUTER_FAILURE_ANALYSIS.md](AUTOROUTER_FAILURE_ANALYSIS.md) — why HALO is hard  

Back to [repo README](../README.md).
