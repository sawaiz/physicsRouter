# Golden board suite

**TL;DR:** Known-good human-routed PCBs → rip copper → autoroute → score against human routing.

## Protocol

1. **Extract** board-level `(segment)` / `(via)` copper as the human golden `RouteResult`
2. **Route** from placement + nets only (human tracks are not search obstacles)
3. **Write** AR copper with tracks/vias cleared (`clear_existing_copper=True`)
4. **Score** completion vs human nets, hard DRC, length/via deltas, layer agreement

Policy: **open nets beat shorts**. Length/via wins only count when AR finishes human nets.

## Run

```bash
# Full suite (routes each board)
physics-router golden-eval --manifest examples/golden/manifest.yaml

# Extract human copper only (no native route — good CI smoke for parsers)
physics-router golden-eval --manifest examples/golden/manifest.yaml --extract-only

# One board, write under viewer/runs/golden_suite/
physics-router golden-eval --id simple_2net --pipeline capacity --effort 0.55

# Optional KiCad oracle (needs kicad-cli)
physics-router golden-eval --id simple_2net --kicad-drc
```

Artifacts per board: `human_route.json`, `ar_route.json`, `*_ar.kicad_pcb`,
`golden_compare.json`, `golden_compare.md`. Suite summary: `suite_results.json`.

## Manifest fields

| Field | Meaning |
|-------|---------|
| `id` | Board key / output folder name |
| `pcb` | Path to human-routed `.kicad_pcb` (relative to repo or manifest dir) |
| `config` | Optional `placement_config.yaml` |
| `expect` | `manufacturing_gate` (full complete + 0 DRC) or `partial_ok` |
| `min_completion` | Soft floor on AR nets / human copper nets (0–1) |
| `timeout_s` | Soft wall-clock warning |
| `difficulty` | Label only (`easy` / `hard`) |

## HEP / CERN / experiment corpus

```bash
# Clone open boards (WREN demo, OpenIPMC, SatNOGS, Jetson, KiCad demos, …)
bash scripts/fetch_golden_boards.sh

# Inventory human copper + charts + physics report
python scripts/golden_corpus_analyze.py
python scripts/golden_corpus_analyze.py --route-easy

# Docs: docs/GOLDEN_CORPUS.md  ·  charts: docs/images/golden/
```

PHENIX/sPHENIX CAD is not public; WREN + OpenIPMC + HALO act as open stress proxies.

## Adding boards

1. Prefer fabbed / DRC-clean human routes with clear licenses.
2. Drop `.kicad_pcb` under `tests/fixtures/golden/` or point at `third_party/golden/`.
3. Add an entry to `manifest.yaml` and `scripts/golden_corpus_analyze.py` `BOARD_PATHS`.
4. Start with `expect: partial_ok` and `min_completion: 0` until the router is reliable.

See: [SOURCES.md](SOURCES.md) · [docs/GOLDEN_CORPUS.md](../../docs/GOLDEN_CORPUS.md) · [DATASETS.md](../../DATASETS.md)
