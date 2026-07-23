# CLI reference

**TL;DR:** `physics-router <command> --help` always works. Most common: `smoke`, `route`, `serve`, `drc`.

```bash
physics-router --version
physics-router --help
```

---

## Everyday

| Command | Purpose |
|---------|---------|
| `serve` | Web UI + job API (`--host` `--port`) |
| `smoke` | Any-board headless: import → route → PCB → DRC gates |
| `route` | Autoroute (capacity / hybrid / topor) |
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
  [--fail-on-grade B] [--nets NET1,NET2] [--guide-only]
```

- `--config` optional if `--pcb` given (auto net import).  
- `--nets` limits which nets are routed.

### `serve`

```bash
physics-router serve --host 127.0.0.1 --port 8765
```

---

## Placement & scoring

| Command | Purpose |
|---------|---------|
| `place` | SA multi-candidate placement on unlocked parts |
| `score` | Score current layout (geometry + proxies) |
| `improve` | Loop place/route until grade + DRC or timeout |

```bash
physics-router place --config c.yaml --pcb b.kicad_pcb --out-pcb placed.kicad_pcb
physics-router improve --config c.yaml --pcb b.kicad_pcb --timeout 120 --grade B
```

---

## Rules & analysis

| Command | Purpose |
|---------|---------|
| `rules` | Dump stackup / netclasses / floors |
| `pre-route` | Congestion / via budget / escape hints |

```bash
physics-router rules --pcb board.kicad_pcb --out-json rules.json
physics-router pre-route --config c.yaml --pcb board.kicad_pcb
```

---

## Golden boards (vs human routing) {#golden}

```bash
physics-router golden-eval [--manifest examples/golden/manifest.yaml] \
  [--id simple_2net] [--pipeline capacity] [--effort 0.55] \
  [--extract-only] [--kicad-drc] [--out-dir DIR] \
  [--rules-profile via_0p45|via_0p6|source|4layer_capability] \
  [--hard-deadline|--soft-timeout] [--cbs-repair|--no-cbs-repair]
```

Rip-and-reroute known-good PCBs and score against extracted human copper
(tracks **and** zone pours). Hard deadline kills hung native search. CI uses
`examples/golden/ci_manifest.yaml`.

See [examples/golden/README.md](../examples/golden/README.md) · [GOLDEN_CORPUS.md](GOLDEN_CORPUS.md).

## FreeRouting {#freerouting}

| Command | Purpose |
|---------|---------|
| `export-dsn` | Specctra DSN (real pad XY, class rules) |
| `import-ses` | SES wires → KiCad PCB |
| `compare-routes` | Metrics: TopoR JSON vs SES/JSON |

```bash
physics-router export-dsn --pcb board.kicad_pcb -o board.dsn
physics-router import-ses --ses board.ses --pcb board.kicad_pcb -o out.kicad_pcb
physics-router compare-routes --topor topor.json --ses board.ses --out comparison.json
```

See also [../examples/demo/FREEROUTING.md](../examples/demo/FREEROUTING.md).

---

## Export & viz

| Command | Purpose |
|---------|---------|
| `export-step` | STEP with copper / zones / silk |
| `export-openems` | OpenEMS geometry + script |
| `render` | KiCad SVG / 3D plots |
| `viewer-data` | Write `viewer_data.json` |
| `dashboard` | HTML score dashboard |
| `route-guide` | Topology guide only (no clearance) |

---

## Environment variables

| Variable | Role |
|----------|------|
| `KICAD_CLI` | Path to `kicad-cli` |
| `KICAD_PYTHON` | Python with `import pcbnew` |
| `PHYSICS_ROUTER_PRESET` | `physics` / `halo-90` / … at server start |
| `PHYSICS_ROUTER_BIN` | Plugin: path to CLI |
| `PHYSICS_ROUTER_TIMEOUT` | Plugin timeout (seconds) |

---

## Full command list

```
compare-routes  dashboard  drc  export-dsn  export-openems  export-step
import-nets  import-ses  improve  init-config  place  pre-route  render
route  route-guide  rules  score  serve  smoke  viewer-data
```

Back: [USER_GUIDE.md](USER_GUIDE.md) · [QUICKSTART.md](QUICKSTART.md) · [README.md](README.md)
