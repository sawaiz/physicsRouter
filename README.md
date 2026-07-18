# physicsRouter

Physics-aware **KiCad placement and TopoR-style free-angle routing** with closed-loop **DRC/ERC**, an interactive control plane, and an optional **C++/OpenCL** core for speed.

Inspired by [TopoR](https://en.wikipedia.org/wiki/TopoR) / [Eremex TopoR](https://www.eremex.com/products/topor/) (gridless free-angle topology) and multi-objective placement research that scores **post-route and physical** quality, not HPWL alone.

| Doc | Contents |
|-----|----------|
| **[DESIGN.md](DESIGN.md)** | Architecture, design decisions, future work |
| **[RESEARCH.md](RESEARCH.md)** | Algorithm survey and bibliography |
| **[docs/TOPOR.md](docs/TOPOR.md)** | Eremex TopoR product model, images, manuals, binary catalog |
| **[docs/ARCHITECTURE_ROUTER.md](docs/ARCHITECTURE_ROUTER.md)** | Topology-first architecture (3 representations, congestion, roadmap) |
| **[DATASETS.md](DATASETS.md)** | Training corpora and conversion paths |

### TopoR-style routing (what we implement)

Matches the reasoning in [docs/TOPOR.md](docs/TOPOR.md) — **not** a reimplementation of commercial TopoR binaries:

| Phase | Behavior |
|-------|----------|
| **Isotropic free-angle** | LOS → isotropic detours + radar scan → A\* |
| **K-homotopy** | Up to K topologically distinct paths per connection (signature dedupe) |
| **High-level planner** | Feature linear policy for net order + per-net K |
| **CBS conflict clusters** | Conflict graph → small-component re-route; optional CP-SAT vias |
| **Elastic geometry** | Continuous shortening + obstacle repulsion after topology fixed |
| **SI / MFG costs** | Crosstalk parallel-run, return path, acute angles, via-near-pad, … |
| **Why this via** | Each via stores blocked layers + alternatives; UI explain panel |
| **Honesty policy** | Soft illegal copper **off** — open edges beat overlaps |
| **UX** | Live **2D** copper while routing; **3D EMS** only on Simulate |

See [docs/ARCHITECTURE_ROUTER.md](docs/ARCHITECTURE_ROUTER.md) for the full three-representation design and literature map.

```bash
# CLI — isotropic TopoR pipeline (auto multi-variant by net count)
physics-router route --config placement_config.yaml --pcb board.kicad_pcb \
  --out route.json --out-pcb routed.kicad_pcb --variants 2
```

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Optional: fast C++ router (OpenCL GPU when available)
bash scripts/build_native.sh
export PYTHONPATH=native/build${PYTHONPATH:+:$PYTHONPATH}

# Control plane (default board: HALO-90 if cloned)
physics-router serve --port 8765
# → http://127.0.0.1:8765/

pytest
python scripts/ci_regression.py
```

### CLI essentials

```bash
physics-router init-config -o placement_config.yaml
physics-router import-nets --pcb board.kicad_pcb --project-dir . -o placement_config.yaml
physics-router place --config placement_config.yaml --pcb board.kicad_pcb --out-pcb placed.kicad_pcb
physics-router route --config placement_config.yaml --pcb placed.kicad_pcb --out-pcb routed.kicad_pcb --drc
physics-router drc --pcb routed.kicad_pcb --out-dir drc_out
physics-router export-step --pcb routed.kicad_pcb -o board_sim.step
physics-router export-dsn --config placement_config.yaml -o board.dsn
```

### HALO-90 test board

```bash
git clone git@github.com:openKolibri/halo-90.git third_party/halo-90
physics-router score \
  --config examples/halo-90/placement_config.yaml \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb
```

- **90 LEDs locked** via `lock_ref_prefixes: ["D"]` (product geometry).
- 4-layer stackup read from KiCad; regions and net weights in `examples/halo-90/`.

---

## What it does

```
YAML / KiCad labels  →  multi-objective place (SA, unlocked parts)
                     →  TopoR pipeline (isotropic free-angle · multi-variant · rubberband)
                     →  write copper to .kicad_pcb
                     →  kicad-cli DRC (+ ERC if schematic present)
                     →  Simulate: GLB 3D + OpenEMS EMI visualization
```

**Policies that matter**

1. Clearance routes do **not** paint illegal straight “soft” copper; open edges beat overlaps.
2. Official **KiCad DRC** is the legality oracle after apply/autoroute.
3. **Routing UX is 2D** (KiCad-style layers). **3D is post-route** on the Simulate step for EMS/OpenEMS.
4. Routing is **isotropic free-angle** (TopoR-style), not Specctra preferred H/V.
5. 2D preview, 3D GLB, and routes share **KiCad millimetre XY** (view may Y-flip for display: hook top, switch left).
6. Native `pr_native` accelerates hot paths when built; Python remains clearance authority for legal copper.

---

## Control plane

```bash
physics-router serve --host 127.0.0.1 --port 8765
```

| Step | UI |
|------|-----|
| Setup | Preset (HALO-90 / synthetic), YAML, locked vs free parts · **2D board** |
| Place | SA on unlocked footprints; physics weights · **2D board** |
| Route | Isotropic TopoR free-angle, multi-variant, **2D only** (no 3D), apply copper |
| Simulate | **3D + OpenEMS EMI** visualization, spice/PI, rebuild GLB |
| Validate | pytest, CI regression, DRC, ERC |

Assets: `viewer/` (UI), `viewer/assets/*.glb` (regenerated locally; large files gitignored).

---

## Native C++ core (optional, v1.1 isotropic)

| Path | Role |
|------|------|
| `native/` | Isotropic free-angle, multi-site vias + reasons, rubberband, via minimize, batch score |
| OpenCL | GPU batch clearance (e.g. Apple M3) |
| OpenMP | Parallel score batches when available |
| `scripts/build_native.sh` | CMake + pybind11 → `pr_native*.so` |
| `./native/build/pr_bench` | Micro-benchmark |

```bash
bash scripts/build_native.sh
export PYTHONPATH=native/build:src
python -c "from physics_router.native_bridge import info; print(info())"
# → 1.1.0-native-isotropic · GPU when OpenCL present
```

Details: [native/README.md](native/README.md). Python still owns K-homotopy / CBS / planner / SI-MFG; native accelerates geometry and can be polished in Python.
---

## Architecture (modules)

| Module | Role |
|--------|------|
| `models` / `config_io` | Net labels, physics weights, YAML |
| `kicad_io` / `design_rules` | Footprints, stackup, DRC floors |
| `placement` / `physics` | SA placement + multi-objective scores |
| `router` / `routing_strategies` / `topology` | Free-angle core, signatures, congestion |
| `homotopy` / `planner` / `conflict_cbs` | K-homotopy, net-order policy, CBS/CP-SAT repair |
| `elastic` / `si_mfg` | Continuous forces; SI + manufacturing cost terms |
| `native_bridge` | Optional C++/OpenCL backend |
| `kicad_tools` | DRC, ERC, STEP/GLB, renders |
| `server` / `viewer` | HTTP API + three.js / 2D UI |
| `dsn_export` / `compare` | Specctra DSN vs FreeRouting metrics |

---

## Requirements

- Python 3.10+
- KiCad 8+ (`kicad-cli`) for DRC/ERC/STEP/GLB on real boards
- Optional: Ngspice, OpenEMS/CSXCAD, CMake 3.16+ for native build

---

## License

MIT — see package metadata. HALO-90 is a separate project; clone under `third_party/` (gitignored).
