# physicsRouter

**Tested topological autorouting for KiCad.**  
Plan connectivity as graph topology, then draw free-angle copper with exact clearance — **open nets beat illegal copper**.

| | |
|---|---|
| **Focus** | Effective, measured multipin / multilayer PCB routing (TopoR-class geometry + PathFinder-style planning) |
| **Geometry** | Required C++ core `pr_native` (ExactMap + free-angle search) |
| **Product** | Python CLI · web UI · net policy · KiCad I/O · DRC · golden benchmarks |
| **Success** | Reachable pads · real vias · full multipin nets · **0 hard DRC** |
| **Not success** | Pretty tracks that short, stub, or only “look” finished |

```text
.kicad_pcb → model → pin-access + capacity mesh + Steiner topology
         → free-angle copper → native DRC → optional KiCad DRC
         → (if complete) SPICE/OpenEMS feedback → re-place / re-topo / pours
```

---

## Flagship benchmark: mppcInterface v1.3

**Primary golden board** — HEP SiPM/MPPC readout ([muonTelescope/mppcInterface](https://github.com/muonTelescope/mppcInterface) @ **`580c61d`**).  
Complete **4-layer human route** (not HEAD’s later 2-layer tree). Design notes cite **sPHENIX-class** bias/coincidence topologies.

| Human golden | Value |
|--------------|------:|
| Size | **65 × 30 mm** |
| Parts / nets | **161 / 85** (all nets have copper) |
| Layers | F · In1 · In2 · B |
| Segments · vias · pours | **1199 · 155 · 61** |
| Track length | **1931.8 mm** |

![mppc human vs AR](docs/images/golden/mppc_v13_compare.png)

![mppc metrics](docs/images/golden/mppc_v13_metrics.png)

![mppc human layers](docs/images/golden/mppc_v13_human_layers.png)

**Full write-up:** [docs/MPPC_BENCHMARK.md](docs/MPPC_BENCHMARK.md) · files under [examples/mppc-interface/](examples/mppc-interface/)

```bash
python scripts/run_mppc_benchmark.py
# or
physics-router route \
  --pcb examples/mppc-interface/mppcInterface_v1.3.kicad_pcb \
  --config examples/mppc-interface/placement_config.yaml \
  --pipeline capacity --effort 0.5
```

**Score policy:** completion vs human nets · hard DRC = 0 · open > short.  
SPICE/OpenEMS proxies apply only after a **fully legal** route (`improve --physics-feedback`).

---

## Scope (what this project is for)

physicsRouter is a **research/engineering autorouter** aimed at:

1. **Topological planning** — hypergraphs, overflow-aware Steiner trees, cut-capacity certificates, DSATUR layer colors, shared pin-escape costing  
2. **Free-angle geometry** — clearance-legal copper (not only 45°/90° grids)  
3. **Honest evaluation** — rip human copper → re-route → score against the original  
4. **Dense / multipin stress** — charlieplex (HALO-90), HEP instruments (mppc), OHL product boards  

It is **not** a full schematic-to-fab suite, and it does not claim commercial TopoR parity on every dense board yet. The goal is **measured** progress under a manufacturing gate.

### Pipeline (capacity / production)

```text
via_profile → pin_access → topology_mesh → global_sections
          → detailed free-angle → manufacturing_gate
```

| Stage | Graph / physics idea |
|-------|----------------------|
| Via profile | Auto 0.60 vs 0.45 mm vias by pad reachability |
| Pin access | Legal offset vias; shared-escape resource map |
| Topology | Crossing-aware / **overflow Steiner** + annular multipin |
| Global sections | Capacity mesh · PathFinder history · shared via charge |
| Detail | C++ ExactMap free-angle search |
| Gate | Full multipin connectivity + 0 native hard DRC |
| Feedback | SPICE + OpenEMS **after** legal complete copper |

Literature map: [RESEARCH.md](RESEARCH.md) (PathFinder, rubber-band, TritonRoute, MLV-CBS, Steiner packing).

---

## Benchmarks & galleries

### mppcInterface (HEP golden)

See top of this page · [docs/MPPC_BENCHMARK.md](docs/MPPC_BENCHMARK.md)

### OHL / open-hardware suite

Rip-and-reroute vs human copper on CERN-OHL and public demos:

![OHL scoreboard](docs/images/golden/ohl_scoreboard.png)

![OHL length](docs/images/golden/ohl_length_compare.png)

| Board | License | Result (latest gallery) |
|-------|---------|-------------------------|
| `simple_2net` | fixture | **A** · 100% · 0 DRC |
| `ecc83_pp` / `_v2` | KiCad demo | **A** · 100% · 0 DRC |
| `ofm_illumination` | CERN-OHL | D · 50% · 0 DRC (honest partial) |
| `openflexure_illum` | CERN-OHL-S | F · 25% · 0 DRC (pour-heavy) |
| Dense OHL (PQ9, OpenIPMC, …) | CERN-OHL / open | often TIMEOUT under hard deadline |

Full table + per-board copper plots: **[examples/golden/RESULTS.md](examples/golden/RESULTS.md)** · [examples/golden/README.md](examples/golden/README.md)

```bash
bash scripts/fetch_golden_boards.sh
python scripts/run_ohl_golden_gallery.py --effort 0.45
```

### HALO-90 (dense charlieplex stress)

In-repo LED earring board: zero-violation **partial** routes by design.  
[examples/halo-90/](examples/halo-90/) · [docs/AUTOROUTER_FAILURE_ANALYSIS.md](docs/AUTOROUTER_FAILURE_ANALYSIS.md)

### Corpus charts

![human scale](docs/images/golden/01_human_scale.png)

![layer strategy](docs/images/golden/02_layer_strategy.png)

More: [docs/GOLDEN_CORPUS.md](docs/GOLDEN_CORPUS.md) · [docs/BENCHMARKS.md](docs/BENCHMARKS.md)

---

## 60-second start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
bash scripts/build_native.sh

# Interactive
physics-router serve --port 8765   # http://127.0.0.1:8765

# Headless any board
physics-router smoke --pcb path/to/board.kicad_pcb --fail-on-drc

# Primary golden
python scripts/run_mppc_benchmark.py

# Tests
pytest -q
```

**Docs:** [docs/QUICKSTART.md](docs/QUICKSTART.md) · [docs/USER_GUIDE.md](docs/USER_GUIDE.md) · [docs/README.md](docs/README.md)

---

## How to use

| Path | When | Command |
|------|------|---------|
| **Viewer** | Import, lock nets, re-route, 3D | `physics-router serve` |
| **CLI** | Scripts / CI | `smoke` · `route` · `golden-eval` · `improve` · `drc` |
| **Plugin** | Inside pcbnew | [kicad_plugins/](kicad_plugins/README.md) |

```bash
# Capacity / topological pipeline
physics-router route --pcb board.kicad_pcb \
  --pipeline capacity --effort 0.55 \
  --out-json route.json --out-pcb routed.kicad_pcb --fail-on-drc

# Score vs human copper
physics-router golden-eval --manifest examples/mppc-interface/manifest.yaml
physics-router golden-eval --manifest examples/golden/ci_manifest.yaml

# Place + route + physics feedback (only after full legal copper)
physics-router improve --config placement_config.yaml --pcb board.kicad_pcb \
  --timeout 180 --grade B --physics-feedback

# FreeRouting baseline interchange
physics-router export-dsn --pcb board.kicad_pcb -o board.dsn
```

---

## What “good” means

1. Every pad of a **committed** net is multilayer-connected  
2. Failed nets leave **no** stubs (open > short)  
3. Layer changes use **real vias** (rule-sized, not point vias)  
4. Native DRC: **zero** hard violations  
5. Prefer KiCad copper DRC on the applied board  
6. Optional: SPICE / OpenEMS proxies **after** (1–5), never instead of them  

---

## Routing process (visual)

![process strip](docs/images/routing_process/6_process_strip.png)

| Stage | Module |
|-------|--------|
| Pad / zone / Edge.Cuts | `kicad_io` |
| Pin-access + shared escapes | `pin_access` |
| Overflow Steiner + cuts | `graph_theory` |
| Capacity mesh + sections | `capacity_mesh` · `global_router` · C++ |
| Free-angle copper | `pr_native` ExactMap |
| Atomic full-net commit | `router` / hybrid policy |
| Physics feedback | `physics_feedback` (SPICE · OpenEMS proxies) |
| Oracle | KiCad `kicad-cli` DRC |

---

## Repo map

| Path | Role |
|------|------|
| [`src/physics_router/`](src/physics_router/) | CLI, server, policy, planning, golden-eval |
| [`native/`](native/) | C++ geometry core |
| [`examples/mppc-interface/`](examples/mppc-interface/) | **Primary golden** (v1.3 PCB) |
| [`examples/golden/`](examples/golden/) | OHL suite · RESULTS · manifests |
| [`examples/halo-90/`](examples/halo-90/) | Dense multipin stress |
| [`docs/`](docs/) | Guides · benches · architecture |
| [`tests/`](tests/) | Unit + golden + graph + physics feedback |

---

## Status

| Area | State |
|------|--------|
| Version | **0.2.0** · native `2.0.0-production-flow` |
| Tests | `pytest -q` (extensive suite; native required for full collection) |
| Synthetic | Full route + 0 DRC typical |
| mppc v1.3 | Pinned complete human golden; AR scored via golden-eval |
| OHL gallery | Easy boards A/partial; dense often hard-deadline TIMEOUT |
| HALO-90 | Legal partials; dense CPX still open research |
| Physics loop | SPICE/OpenEMS after complete legal copper only |

---

## Documentation

| Doc | Content |
|-----|---------|
| [docs/MPPC_BENCHMARK.md](docs/MPPC_BENCHMARK.md) | **Flagship human vs AR report** |
| [examples/golden/RESULTS.md](examples/golden/RESULTS.md) | OHL gallery table + plots |
| [docs/GOLDEN_CORPUS.md](docs/GOLDEN_CORPUS.md) | Corpus + metrics + physics |
| [docs/CAPACITY_MESH.md](docs/CAPACITY_MESH.md) | Pipeline stages |
| [DESIGN.md](DESIGN.md) | Architecture decisions |
| [RESEARCH.md](RESEARCH.md) | Papers · graph theory · TopoR |
| [docs/CLI.md](docs/CLI.md) | Full CLI |
| [docs/AUTOROUTER_FAILURE_ANALYSIS.md](docs/AUTOROUTER_FAILURE_ANALYSIS.md) | HALO failure lessons |

---

## Requirements · license

- Python **3.10+**, CMake **3.16+**, C++17  
- KiCad **8+** (`kicad-cli`) for authoritative DRC  
- Optional: OpenCL, Ngspice, OpenEMS  

**MIT** for physicsRouter.  
Third-party boards (mppcInterface, HALO-90, OHL clones under `third_party/`) keep their own licenses — see [examples/golden/SOURCES.md](examples/golden/SOURCES.md).
