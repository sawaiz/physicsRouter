# Quick start

**TL;DR:** install → build native → `route` (native progress window) or `smoke` (headless).

---

## 1. Install

```bash
git clone <this-repo> physicsRouter
cd physicsRouter
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
bash scripts/build_native.sh
```

Check the C++ core:

```bash
python -c "from physics_router.native_bridge import info; print(info())"
# expect: available=True, version ~ 2.0.0-production-flow
```

| Need | Optional? |
|------|-----------|
| Python 3.10+ | required |
| CMake 3.16+, C++17 | required for native |
| KiCad 8+ (`kicad-cli`) | for DRC / STEP |
| OpenCL | faster on Apple GPU / some GPUs |
| tkinter | native progress window (usually bundled) |

---

## 2. Route a board (pick one)

### A) Route with native progress window

```bash
physics-router route --pcb /path/to/board.kicad_pcb \
  --out-json route.json --out-pcb routed.kicad_pcb
```

A desktop window shows stage progress, log lines, and live copper as nets commit.
Use **`--no-ui`** for CI / SSH / no display.

### B) One command (CI / scripts)

```bash
physics-router smoke --pcb /path/to/board.kicad_pcb --fail-on-drc
```

Does: auto-import nets → capacity/hybrid route → write under `viewer/runs/smoke_<name>/` → DRC → non-zero exit if DRC fails.

### C) Explicit pipeline flags

```bash
physics-router route --pcb board.kicad_pcb \
  --out-json route.json --out-pcb routed.kicad_pcb \
  --pipeline capacity --effort 0.55 \
  --fail-on-drc --fail-on-unrouted
```

`--config` is optional: without it, nets are imported from the PCB (+ schematic if present).

---

## 3. Example boards

| Example | How |
|---------|-----|
| Synthetic demo | `physics-router route --config examples/demo/…` or smoke with config only |
| HALO-90 | Clone into `third_party/halo-90` — see [../examples/halo-90/README.md](../examples/halo-90/README.md) |
| Muon3 / physics | Sibling `../physics` + [../examples/physics/README.md](../examples/physics/README.md) |
| mppcInterface | [../examples/mppc-interface/](../examples/mppc-interface/) flagship golden |

```bash
# HALO if present
physics-router smoke \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb \
  --config examples/halo-90/placement_config.yaml \
  --min-grade F --no-fail-on-drc   # dense: allow partial for smoke timing
```

---

Next: [USER_GUIDE.md](USER_GUIDE.md) · [CLI.md](CLI.md) · [README.md](../README.md)
