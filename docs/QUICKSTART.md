# Quick start

**TL;DR:** install → build native → `serve` or `smoke` → open / route a `.kicad_pcb`.

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

---

## 2. Route a board (pick one)

### A) Web UI (easiest)

```bash
physics-router serve --host 127.0.0.1 --port 8765
```

Open **http://127.0.0.1:8765**

1. **Board** — drop any `.kicad_pcb`, paste a path, or click an example (HALO-90 / synthetic)  
2. **Route** — **Route** or **Improve until goal**  
3. **Apply to PCB** — write copper  
4. **Check** — KiCad DRC / tests  

Optional on Route: lock nets (keep copper), re-route only selected nets, add keep-out boxes.

### B) One command (CI / scripts)

```bash
physics-router smoke --pcb /path/to/board.kicad_pcb --fail-on-drc
```

Does: auto-import nets → capacity/hybrid route → write `viewer/runs/smoke_<name>/` → DRC → non-zero exit if DRC fails.

### C) Explicit route

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
| Synthetic demo | UI example chip **Synthetic**, or omit `--pcb` with a config |
| HALO-90 | Clone into `third_party/halo-90` — see [../examples/halo-90/README.md](../examples/halo-90/README.md) |
| Muon3 / physics | Sibling `../physics` + [../examples/physics/README.md](../examples/physics/README.md) |

```bash
# HALO if present
physics-router smoke \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb \
  --config examples/halo-90/placement_config.yaml \
  --min-grade F --no-fail-on-drc   # dense: allow partial for smoke timing
```

---

## 4. Tests

```bash
pytest -q
# ~209 tests; native must be built
```

---

## 5. Common failures

| Symptom | Fix |
|---------|-----|
| `pr_native` / native not available | `bash scripts/build_native.sh` |
| `kicad-cli not found` | Install KiCad or set `KICAD_CLI` |
| smoke exits 2 | DRC failed — inspect `.../drc/drc.json` |
| smoke exits 3 | unrouted nets (`--fail-on-unrouted`) |
| smoke exits 4 | grade below `--min-grade` |
| Empty Muon3 shell board | Use telescope v10 path (see physics example README) |

---

## Next

- Day-to-day workflows → [USER_GUIDE.md](USER_GUIDE.md)  
- CLI reference → [CLI.md](CLI.md)  
- Design principles → [../DESIGN.md](../DESIGN.md)  
- Doc map → [README.md](README.md)
