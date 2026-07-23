# User guide

**TL;DR:** two interfaces — **CLI** (native live-progress window when routing) and **KiCad plugin**. Same router underneath.

---

## 1. Native progress window

When you run `physics-router route` (without `--no-ui`), a desktop window opens:

| Area | What you see |
|------|----------------|
| **Header** | Stage name, status, progress bar, elapsed time |
| **Canvas** | Pads (board anchors) + copper segments as nets commit |
| **Log** | Per-stage / per-net progress lines |

Close the window only after routing finishes (the **Close** button enables when done).

```bash
# Live UI while routing
physics-router route --pcb board.kicad_pcb \
  --out-json route.json --out-pcb routed.kicad_pcb

# Headless (CI / SSH / no display)
physics-router route --pcb board.kicad_pcb --no-ui --out-json route.json
```

Requires **tkinter** (usually bundled with Python). If tkinter is missing, routing continues headless.

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
physics-router import-ses --ses board.ses --pcb board.kicad_pcb -o routed.kicad_pcb
```

### Golden eval (human copper as oracle)

```bash
physics-router golden-eval --manifest examples/mppc-interface/manifest.yaml
physics-router golden-eval --manifest examples/golden/ci_manifest.yaml
```

---

## 3. KiCad plugin

See [../kicad_plugins/README.md](../kicad_plugins/README.md). The plugin runs headless smoke/route and reloads copper into pcbnew.

---

## 4. Lock nets / keep-outs / selective re-route

Policy is currently via **config YAML** and CLI:

| Goal | How |
|------|-----|
| Route only some nets | `physics-router route --nets NET1,NET2 …` |
| Locked / fixed copper | Net labels with `locked: true` in placement config |
| Keep-outs | `keepouts` in placement config |

The former web control plane (`serve`) is removed.

---

## 5. OpenEMS / physics

OpenEMS and SPICE proxies run only after a **fully legal** route (complete nets, 0 hard DRC). See `export-openems` and `improve --physics-feedback` in [CLI.md](CLI.md).

Back: [QUICKSTART.md](QUICKSTART.md) · [CLI.md](CLI.md) · [README.md](../README.md)
