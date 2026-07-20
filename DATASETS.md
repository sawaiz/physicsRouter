# PCB datasets for placement & routing training

**TL;DR:** Best ready routing set = **PCBench**. Largest public KiCad corpus = **Open Schematics**. Always check licenses. Gerbers alone lack nets — prefer `.kicad_pcb` + connectivity.

For *using* physicsRouter on any board today: [docs/QUICKSTART.md](docs/QUICKSTART.md) (`smoke --pcb …`).

Curated sources of **KiCad** boards and related formats for supervised / RL training.

> **License note:** Record per-project licenses. Prefer explicit OSHW licenses. Gerbers alone are usually *not* enough for placement training (no netlist / pad nets).

---

## Priority sources (start here)

| Source | Format | Scale | Best for | Link |
|--------|--------|------:|----------|------|
| **PCBench** | Native `.kicad_pcb` + `final.json` (PCB-RDL) | ~164 boards | **Routing ML / RL** (cleaned, ready) | [github.com/PCBench/PCBench](https://github.com/PCBench/PCBench) |
| **Open Schematics** | `.kicad_sch` / `.kicad_pcb` (+ some Altium) | ~88k rows (~317 GB) | **Large-scale** place/route + multimodal | [huggingface.co/datasets/bshada/open-schematics](https://huggingface.co/datasets/bshada/open-schematics) |
| **PCB-Bench (ICLR 2026)** | OSHWHub designs + screenshots / QA | 174 full projects + ~4k QA | Reasoning benchmarks; design understanding | [github.com/digailab/PCB-Bench](https://github.com/digailab/PCB-Bench) |
| **PCBBenchmarks (ASP-DAC/DAC)** | KiCad benchmarks for automated layout | Small curated set | BGA / layout metrics, fanout research | [github.com/aspdac-submission-pcb-layout/PCBBenchmarks](https://github.com/aspdac-submission-pcb-layout/PCBBenchmarks) |
| **RL_PCB** | KiCad + training circuits | ~6 circuits (+ tools) | **Placement RL** baselines | [github.com/LukeVassallo/RL_PCB](https://github.com/LukeVassallo/RL_PCB) |
| **KiCad official demos** | Full projects | Dozens | Clean, versioned reference boards | [gitlab.com/kicad/code/kicad/-/tree/master/demos](https://gitlab.com/kicad/code/kicad/-/tree/master/demos) |
| **RepoRecon (KiCad index)** | GitHub repo index (JSON) | 30k+ KiCad-related repos | **Crawl seed** for more `.kicad_pcb` | [devbisme.github.io/RepoRecon](https://devbisme.github.io/RepoRecon/) · [github.com/devbisme/RepoRecon](https://github.com/devbisme/RepoRecon) |

### PCBench (best ready-made routing set)

- Each board under `PCBs/{author}_{name}/`:
  - `raw.kicad_pcb` — original
  - `processed.kicad_pcb` — cleaned
  - `final.json` — **PCB-RDL** (routing problem + solution for ML)
  - `metadata.json`, `visual.png`
- Includes data-augmentation scripts and an RL env (`PCBRoutingEnv`).
- MIT-licensed dataset packaging; underlying designs come from open-source projects (check `metadata`).

```bash
git clone https://github.com/PCBench/PCBench.git
# Boards: PCBench/PCBs/*/processed.kicad_pcb and final.json
```

### Open Schematics (largest public KiCad corpus)

Hugging Face dataset with paired schematic + PCB text and images:

| Field | Content |
|-------|---------|
| `schematic` | Raw `.kicad_sch` / `.sch` / `.SchDoc` |
| `pcb_files` | List of raw `.kicad_pcb` (etc.) |
| `pcb_images` / `schematic_image` | Rendered PNGs when available |
| `components_used` | Symbol names (KiCad) |
| `name`, `description` | Source repo metadata |

```python
from datasets import load_dataset
ds = load_dataset("bshada/open-schematics", split="train", streaming=True)
# Filter rows where pcb_files is non-empty and extensions include .kicad_pcb
```

**Caveats:** Quality varies; ~55% of PCB images missing; filter by board size, layer count, and DRC cleanliness before training.

### PCB-Bench (ICLR 2026)

- Task 3: **174** complete projects from [OSHWHub](https://oshwhub.com/) (JLCPCB ecosystem) with schematics, layout, libraries, screenshots.
- Also text/image QA on placement & routing — useful for evaluation, less so as pure geometric labels.
- Project page: https://digailab.github.io/PCB-Bench/

### RL_PCB placement set

- End-to-end RL placement; ships a small multi-circuit dataset under `dataset/`.
- Uses real `.kicad_pcb` parsing; post-placement A* routing for wirelength metrics.
- Good for placement baselines and reward design, not for scale.

---

## Large open-hardware crawls (KiCad-native)

These are the practical way to grow beyond curated academic sets.

| Source | Notes |
|--------|--------|
| **RepoRecon** `docs/kicad.json` | Nightly index of GitHub repos tagged/mentioning KiCad; clone and filter for repos containing `*.kicad_pcb` |
| **GitHub code search** | `extension:kicad_pcb`, `path:*.kicad_pcb`, topic `kicad` / `kicad-pcb` |
| **kicad.org/made-with-kicad** | Showcase projects with full source links |
| **CircuitSnips** | 4k+ KiCad **schematic** snippets ([circuitsnips.com](https://www.circuitsnips.com/), [github.com/MichaelAyles/kicad-library](https://github.com/MichaelAyles/kicad-library)) — good for connectivity graphs, not full routed boards |
| **OSHWHub / OSHWLab** | EasyEDA-centric public projects; often have Gerbers + source ([oshwhub.com](https://oshwhub.com/), [oshwlab.com](https://oshwlab.com/)) |
| **OSHWA certified list** | Licensed open hardware with design-file links ([certification.oshwa.org](https://certification.oshwa.org/list.html)) |
| **OHWR / CERN** | Scientific boards (often KiCad or convertible) |

### Suggested GitHub harvest query (manual or API)

```text
extension:kicad_pcb
filename:*.kicad_pcb path:/
topic:kicad
```

Filter clones to projects that include **both** `*.kicad_sch` and a **routed** `*.kicad_pcb` (tracks present).

---

## Other EDA formats → convert to KiCad

| Input format | How to convert | Quality / notes |
|--------------|----------------|-----------------|
| **EAGLE** `.sch` / `.brd` | KiCad: *File → Import Non-KiCad Project → EAGLE* | Built-in; layer mapping may need fixes |
| **EasyEDA / OSHWLab JSON** | [wokwi/easyeda2kicad](https://github.com/wokwi/easyeda2kicad) · [online tool](https://wokwi.com/tools/easyeda2kicad) · [easyeda2kicad6](https://github.com/yaybee/easyeda2kicad6) | Good for bulk OSHWLab harvest |
| **Altium** `.PcbDoc` / `.SchDoc` | KiCad import (recent versions) or [altium2kicad](https://github.com/thesourcerer8/altium2kicad) | Lossy; verify footprints/nets |
| **Specctra DSN / SES** | Freerouting ecosystem; KiCad *Export Specctra DSN* / SES import | Ideal intermediate for routers (problem = DSN, solution = SES) |
| **PADS / P-CAD ASCII** | Historical TopoR I/O; limited free converters | Prefer DSN as hub if available |

### Intermediate format for routers

```
.kicad_pcb  ──export──►  .dsn  ──freerouting──►  .ses  ──import──►  .kicad_pcb
     ▲                      │
     │                      └── also useful as training “problem” representation
     └── placement + copper as ground-truth labels
```

- **DSN** = unrouted (or partially routed) design for autorouters  
- **SES** = session / routing result  
- Tools: [freerouting](https://github.com/freerouting/freerouting), [tscircuit/dsn-converter](https://github.com/tscircuit/dsn-converter)

---

## Gerber datasets (routed copper only)

Gerbers are **fabrication outputs** of already-routed boards. Useful for:

- Learning copper geometry, layer stacks, DFM patterns  
- Physics / SI (e.g. [antmicro/gerber2ems](https://github.com/antmicro/gerber2ems) → OpenEMS)  
- Reconstruction / reverse-engineering experiments  

**Not sufficient alone** for place-and-route training (missing explicit nets, pad→net maps, clearances as design rules).

| Source | Notes |
|--------|--------|
| **Project fab folders** | Many OSHW repos ship `gerbers/`, `production/`, `CAM/` next to KiCad |
| **OSHWHub / OSHWLab** | Public projects often include Gerber downloads |
| **OSH Park shared projects** | Community boards (mixed formats) |
| **Hackaday.io / Hackster** | Attached Gerber zips with hardware projects |
| **Individual orgs** | e.g. Adafruit, SparkFun, Framework (where license allows) |

**Pairing strategy:** Prefer samples where **both** source CAD and Gerbers exist; use Gerbers as an extra modality or physics label, and CAD for nets/placement/tracks.

```bash
# From a cleaned .kicad_pcb, regenerate Gerbers for consistency:
# KiCad CLI (version-dependent flags):
kicad-cli pcb export gerbers --output ./gerbers board.kicad_pcb
kicad-cli pcb export drill   --output ./gerbers board.kicad_pcb
```

---

## Related datasets (not board place/route)

| Name | Domain | Why listed |
|------|--------|------------|
| [CircuitNet](https://circuitnet.github.io/) | **VLSI / ASIC** RTL→layout | Same EDA ML ideas; **not** PCB |
| DeepPCB / PCB defect sets | Manufacturing **AOI** images | Inspection, not routing |
| GraphPCB | Graph rep of PCB images | Vision / graph ML |
| PCBRouteNet (paper) | ~300 board tasks (GitHub/OSHWLab) | Research dataset for ML routing; check paper for release status |

---

## Recommended training corpus layout

```
data/
  raw/
    kicad/                 # cloned or HF-extracted projects
    eagle/ easyeda/ ...    # pre-conversion
    gerber/                # fab outputs (optional modality)
  converted/
    kicad_v8/              # normalized KiCad version
    dsn/                   # Specctra problems
    ses/                   # routed sessions (when available)
  labels/
    placement/             # footprints: ref, xy, rot, layer
    routing/               # tracks, vias, nets (or PCB-RDL JSON)
    pairs/                 # unrouted_problem → routed_solution
  meta/
    licenses.csv
    stats.parquet          # layers, nets, pads, track length, via count
```

### Label extraction (from `.kicad_pcb`)

| Task | Inputs | Targets |
|------|--------|---------|
| Placement | netlist + footprint sizes + board outline | `(x, y, rotation)` per refdes |
| Routing | placed board + nets + design rules | tracks/vias (or sequence of actions) |
| Joint | schematic/netlist only | full place + route |

**Supervision tricks used in literature:**

- Drop all tracks/vias → problem; keep copper → solution (PCBench style)  
- Subsample nets for augmentation (`augmentation.py` in PCBench)  
- Multi-candidate routing (TopoR-like) if you generate alternatives with Freerouting / FreeRouting seeds  

---

## Conversion & tooling checklist

| Tool | Role |
|------|------|
| KiCad 8+ (GUI + `kicad-cli`) | Import EAGLE/Altium, export DSN/Gerber, version normalize |
| [kiutils](https://github.com/mvnmgrx/kiutils) | Parse/write KiCad files in Python |
| [easyeda2kicad](https://github.com/wokwi/easyeda2kicad) | EasyEDA/OSHWLab → `.kicad_pcb` |
| [freerouting](https://github.com/freerouting/freerouting) | DSN→SES autoroute baseline / weak labels |
| [tscircuit/dsn-converter](https://github.com/tscircuit/dsn-converter) | Circuit JSON ↔ DSN |
| [gerber2ems](https://github.com/antmicro/gerber2ems) | Gerber → OpenEMS (physics labels) |
| [RepoRecon](https://github.com/devbisme/RepoRecon) | Discover GitHub KiCad repos at scale |

---

## Practical “v1 corpus” plan

1. **Seed (days):** Clone PCBench (~164) + KiCad demos + ASP-DAC PCBBenchmarks.  
2. **Scale (weeks):** Stream/filter Open Schematics for rows with non-empty `pcb_files` and plausible track count.  
3. **Convert:** Bulk EasyEDA/OSHWLab via easyeda2kicad; EAGLE via KiCad import.  
4. **Normalize:** Re-save all boards with one KiCad version; run DRC filter; export DSN unrouted variants.  
5. **Gerber modality:** Export Gerbers from the same normalized boards for SI/physics and multi-modal models.  
6. **Index:** Store license, source URL, layer count, #nets, #pads, total track length, via count in `meta/stats.parquet`.  
7. **Holdout:** Reserve high-quality boards (e.g. KiCad demos + starred OSHW) for evaluation only.

---

## Quick links

- PCBench: https://github.com/PCBench/PCBench  
- Open Schematics: https://huggingface.co/datasets/bshada/open-schematics  
- PCB-Bench: https://github.com/digailab/PCB-Bench  
- PCBBenchmarks: https://github.com/aspdac-submission-pcb-layout/PCBBenchmarks  
- RL_PCB: https://github.com/LukeVassallo/RL_PCB  
- KiCad demos: https://gitlab.com/kicad/code/kicad/-/tree/master/demos  
- RepoRecon: https://devbisme.github.io/RepoRecon/  
- Freerouting: https://github.com/freerouting/freerouting  
- EasyEDA→KiCad: https://github.com/wokwi/easyeda2kicad  
- OSHWHub: https://oshwhub.com/  
- TopoR (routing paradigm reference): https://en.wikipedia.org/wiki/TopoR  
- Eremex TopoR product + image/doc cache: [docs/TOPOR.md](docs/TOPOR.md) · https://www.eremex.com/products/topor/  
- TopoR 6.1 user manual (public PDF): https://www.eremex.com/support/documentation/461750.pdf  
 
