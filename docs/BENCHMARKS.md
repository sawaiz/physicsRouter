# Benchmarks & images

**TL;DR:** numbers below are from a typical Apple M3 + OpenCL run; re-run scripts to refresh. Prefer [bench_latest.json](images/routing_process/bench_latest.json) for exact artifacts.

---

## How to regenerate

```bash
bash scripts/build_native.sh
python scripts/render_routing_process.py --halo   # stage images + bench JSON
python scripts/generate_docs_images.py            # placement / score plots
```

---

## Process gallery

| Stage | Synthetic | HALO-90 |
|-------|-----------|---------|
| Placement + outline | ![s1](images/routing_process/synthetic_1_placement_outline.png) | ![h1](images/routing_process/halo90_1_placement_outline.png) |
| Guide topology | ![s2](images/routing_process/synthetic_2_guide_topology.png) | ![h2](images/routing_process/halo90_2_guide_topology.png) |
| Clearance copper | ![s3](images/routing_process/synthetic_3_clearance_raw.png) | ![h3](images/routing_process/halo90_3_clearance_raw.png) |
| Re-geometry | ![s4](images/routing_process/synthetic_4_regeometry.png) | ![h4](images/routing_process/halo90_4_regeometry.png) |
| By layer | ![s5](images/routing_process/synthetic_5_by_layer.png) | ![h5](images/routing_process/halo90_5_by_layer.png) |
| DRC map | ![s7](images/routing_process/synthetic_7_drc_map.png) | ![h7](images/routing_process/halo90_7_drc_map.png) |

**Strip:**

![process](images/routing_process/6_process_strip.png)

More: [images/routing_process/README.md](images/routing_process/README.md)

---

## Representative timings (M3)

| Workload | Time | Result |
|----------|-----:|--------|
| Synthetic native `route_board` | ~3 ms | segs + vias, capacity mesh on |
| Synthetic `clearance_aware_route` | ~5 ms | 0 shorts typical |
| Synthetic capacity pipeline | ~100 ms | full nets typical |
| HALO guide topology | ~0.2 s | multipin guide |
| HALO clearance (full docs render) | ~minutes | legal partials common |
| HALO native sequential bench | tens of s | 0 shorts on committed copper |

### HALO honest status

Under **atomic full-net + zero hard violation**:

- Committed copper aims for **0 shorts**  
- Many nets may stay **unrouted** rather than partially stubbed  
- Dense charlieplex completion is the open stress goal  

See [AUTOROUTER_FAILURE_ANALYSIS.md](AUTOROUTER_FAILURE_ANALYSIS.md).

---

## Other plots

![placement](images/placement_overview.png)
![score](images/score_breakdown.png)
![runtimes](images/runtimes.png)
![guide](images/route_guide.png)
![layers](images/route_by_layer.png)

---

## Tests

```bash
pytest -q
# ~209 collected when native is built
```

Back: [README.md](README.md) · [../README.md](../README.md)
