# User guide

**TL;DR:** three interfaces — **web viewer**, **CLI**, **KiCad plugin**. Same router underneath.

---

## 1. Web viewer

```bash
physics-router serve --port 8765
# http://127.0.0.1:8765
```

### Steps

| Step | What you do |
|------|-------------|
| **1 · Board** | Drop `.kicad_pcb`, open a filesystem path, or pick an example chip |
| **2 · Place** | Optional — move unlocked parts only |
| **3 · Route** | Route / improve; lock nets; re-route selection; keep-outs |
| **4 · 3D** | Inspect model; OpenEMS only if route gate passes |
| **5 · Check** | DRC, unit tests, pipeline |

### Board import

- **Drop / browse** → `POST /api/board/import` (file text uploaded)  
- **Path** → `POST /api/board/open` (server reads disk)  
- Nets auto-import from PCB netclasses (+ schematic labels if found)  
- Zones/pours become routing obstacles (same net may pass)

### Route panel controls

| Control | Effect |
|---------|--------|
| **Lock** (left checkbox) | Keep existing copper; do not re-route that net |
| **Re-route only** (right) | Route just those nets; others seed as obstacles if locked |
| **Keep-out** x1,y1,x2,y2 | Rectangle blocked for all nets (board mm) |
| **Improve until goal** | Loop until grade / DRC / timeout |
| **Apply to PCB** | Write segments/vias into a session `.kicad_pcb` |

API for policy: `POST /api/routing/policy` with `locked_nets`, `reroute_nets`, `keepouts`.

### OpenEMS / 3D gate

OpenEMS job runs only if:

- a route exists, and  
- grade ≥ **C** (default), and  
- clearance violations = 0  

Check **Force export** in the UI (or `force=1` on the job) to override.

---

## 2. CLI workflows

Full flag list: [CLI.md](CLI.md).

### Headless CI on any board

```bash
physics-router smoke --pcb board.kicad_pcb --fail-on-drc --min-grade D
echo $?   # 0 ok · 2 DRC · 3 unrouted · 4 grade
```

### Place → route → DRC

```bash
physics-router import-nets --pcb board.kicad_pcb -o config.yaml
physics-router place --config config.yaml --pcb board.kicad_pcb --out-pcb placed.kicad_pcb
physics-router route --config config.yaml --pcb placed.kicad_pcb \
  --out-pcb routed.kicad_pcb --fail-on-drc
physics-router drc --pcb routed.kicad_pcb
```

### FreeRouting interop

```bash
physics-router export-dsn --pcb board.kicad_pcb -o board.dsn
# run FreeRouting → board.ses
physics-router import-ses --ses board.ses --pcb board.kicad_pcb -o board_fr.kicad_pcb
physics-router compare-routes --topor topor.json --ses board.ses --out comparison.json
```

### Continuous improve

```bash
physics-router improve --config config.yaml --pcb board.kicad_pcb \
  --timeout 120 --grade B --out-json improve.json
```

---

## 3. KiCad plugin

1. Install so `physics-router` is on `PATH` (or set `PHYSICS_ROUTER_BIN`).  
2. Copy `kicad_plugins/physics_router_action.py` into KiCad’s plugins folder.  
3. pcbnew → **Tools → External Plugins → physicsRouter: Auto-route**.  

Details: [../kicad_plugins/README.md](../kicad_plugins/README.md).

---

## 4. Config (`placement_config.yaml`)

Optional for `route` / `smoke` when you pass `--pcb` (auto-import fills nets).

| Field | Meaning |
|-------|---------|
| `nets[]` | `name`, `net_class`, `weight`, `critical`, `pair_with`, `max_length_mm`, `track_width_mm`, `locked` |
| `fixed[]` | Forced footprint pose |
| `lock_ref_prefixes` | e.g. `["D"]` lock all LED refs |
| `keepouts[]` | Rect obstacles `{x1,y1,x2,y2}` |
| `locked_nets` | Nets not re-routed |
| `physics` | Placement score weights |

```bash
physics-router init-config -o placement_config.yaml
physics-router import-nets --pcb board.kicad_pcb -o placement_config.yaml
```

---

## 5. HTTP API (control plane)

Base: `http://127.0.0.1:8765`

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | version + native info |
| GET | `/api/snapshot` | session, nets, locks, jobs |
| POST | `/api/preset` | `{ "preset": "halo-90" }` |
| POST | `/api/board/open` | `{ "pcb_path": "..." }` |
| POST | `/api/board/import` | `{ "pcb_text": "...", "filename": "..." }` |
| POST | `/api/routing/policy` | locks / keepouts / re-route list |
| POST | `/api/jobs` | `{ "type": "route_topor", "params": {} }` |
| GET | `/api/jobs/{id}` | progress / result |
| GET | `/api/viewer-data` | 2D/3D payload |

Job types include: `route_topor`, `improve`, `place`, `apply_route_pcb`, `drc`, `openems`, `export_dsn`, `export_board_3d`, `tests`, `pipeline`.

---

## 6. Presets & env

| Preset / env | Board |
|--------------|--------|
| `halo-90` | `third_party/halo-90` wearable |
| `physics` / `PHYSICS_ROUTER_PRESET=physics` | Muon3 telescope layout |
| `synthetic` | Built-in demo |
| import / custom | Any PCB via UI or API |

---

## 7. Tips for dense boards

1. Prefer **zero-violation partial** over forcing completion.  
2. Lock power/ground pours’ nets after a good pass; re-route signal islands.  
3. Use fab profile **capability** floors when recommended spacing is too fat.  
4. BGA/QFN get denser pin-access automatically from footprint name / pad count.  
5. Existing copper zones block foreign nets — good for pours.

---

## Next

- [CLI.md](CLI.md) · [CAPACITY_MESH.md](CAPACITY_MESH.md) · [../DESIGN.md](../DESIGN.md) · [README.md](README.md)
