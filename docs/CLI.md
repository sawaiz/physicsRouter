# CLI reference

**TL;DR:** `physics-router <command> --help` always works. Most common: `smoke`, `route`, `drc`, `golden-eval`.

```bash
physics-router --version
physics-router --help
```

---

## Everyday

| Command | Purpose |
|---------|---------|
| `route` | Autoroute (capacity / hybrid / topor); **opens native progress window** unless `--no-ui` |
| `smoke` | Any-board headless: import → route → PCB → DRC gates |
| `golden-eval` | Rip human copper on golden boards → autoroute → score vs human |
| `drc` | Official KiCad DRC only |
| `import-nets` | Build `placement_config.yaml` from PCB/sch |
| `init-config` | Write example YAML |

### `smoke`

```bash
physics-router smoke --pcb board.kicad_pcb \
  [--config placement_config.yaml] \
  [--out-dir DIR] [--out-pcb PATH] [--out-json PATH] \
  [--effort 0.45] [--timeout 120] \
  [--fail-on-drc/--no-fail-on-drc] \
  [--fail-on-unrouted/--allow-unrouted] \
  [--min-grade D]
```

| Exit | Meaning |
|-----:|---------|
| 0 | Passed gates |
| 2 | DRC fail / no kicad-cli when required |
| 3 | Unrouted nets (if enforced) |
| 4 | Grade worse than `--min-grade` |

### `route`

```bash
physics-router route [--config YAML] --pcb board.kicad_pcb \
  [--out-json route.json] [--out-pcb routed.kicad_pcb] \
  [--pipeline auto|capacity|hybrid|topor] [--effort 0.55] \
  [--clearance MM] [--grid MM] [--variants N] \
  [--drc/--no-drc] [--fail-on-drc] [--fail-on-unrouted] \
  [--fail-on-grade B] [--nets NET1,NET2] [--guide-only] \
  [--no-ui]
```

- `--config` optional if `--pcb` given (auto net import).  
- `--nets` limits which nets are routed.  
- **Native progress window** opens by default (stage bar + copper canvas).  
- `--no-ui` — headless (CI / SSH / no display).

### `serve` (removed)

The web control plane was removed. Use `route` (native window) or `route --no-ui` / `smoke` for headless work.

---

## Placement & scoring

| Command | Purpose |
|---------|---------|
| `place` | SA multi-candidate placement on unlocked parts |
| `score` | Score current layout (geometry + proxies) |
| `improve` | Loop place/route until grade + DRC or timeout |

```bash
physics-router place --config c.yaml --pcb b.kicad_pcb --out-pcb placed.kicad_pcb
physics-router improve --config c.yaml --pcb b.kicad_pcb --timeout 120 --grade B \
  --physics-feedback --physics-export-dir /tmp/em_rounds
```

After a **fully legal** route (0 hard DRC, complete nets), SPICE + OpenEMS
proxies score the copper and feed the next round.

---

## Import / export

| Command | Purpose |
|---------|---------|
| `import-nets` | PCB (+ optional sch) → YAML net labels |
| `export-dsn` | FreeRouting Spef-style DSN |
| `import-ses` | FreeRouting SES → segments |
| `export-step` / `export-openems` | 3D / EM mesh proxies |
| `viewer-data` | JSON board snapshot for tooling |

```bash
physics-router import-nets --pcb board.kicad_pcb -o placement_config.yaml
physics-router export-dsn --pcb board.kicad_pcb -o board.dsn
physics-router export-step --pcb board.kicad_pcb -o board.step
```

---

## Other

| Command | Purpose |
|---------|---------|
| `compare-routes` | Diff two route JSONs |
| `dashboard` | HTML score dashboard |
| `route-guide` | Topology guide only (no clearance) |
| `rules` | Dump design rules from PCB |
| `pre-route` | Pin-access / plan preview |
| `render` | Simple 2D render of routes |

---

## Environment variables

| Variable | Role |
|----------|------|
| `KICAD_CLI` | Path to `kicad-cli` |
| `KICAD_PYTHON` | Python with `import pcbnew` |
| `PHYSICS_ROUTER_BIN` | Plugin: path to CLI |
| `PHYSICS_ROUTER_TIMEOUT` | Plugin timeout (seconds) |

---

## Full command list

```
compare-routes  dashboard  drc  export-dsn  export-openems  export-step
import-nets  import-ses  improve  init-config  place  pre-route  render
route  route-guide  rules  score  serve (removed)  smoke  viewer-data
```

Back: [USER_GUIDE.md](USER_GUIDE.md) · [QUICKSTART.md](QUICKSTART.md) · [README.md](../README.md)
