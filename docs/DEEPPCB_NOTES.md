# Lessons from DeepPCB (applied to physicsRouter)

Source material (user link resolved to DeepPCB essays):

- [The 60-Year Routing Problem Nobody Solved](https://deeppcb.ai/the-60-year-routing-problem-nobody-solved/)  
  (also on X: [DeepPCB article](https://x.com/DeepPCB/article/2039788592986308693))
- [The Autorouter Broke Your Trust](https://deeppcb.ai/the-autorouter-broke-your-trust-heres-whats-actually-different-now/)
- [PCB Autorouter Benchmark 2026](https://deeppcb.ai/pcb-autorouter-benchmark-2026/)

## Core thesis (what we take seriously)

1. **Routing is not pure pathfinding.**  
   It is *resource allocation wrapped in geometry wrapped in physics*. Lee’s maze finds A→B if a path exists; it does **not** reason about whether that path makes later nets impossible.

2. **Static heuristic priorities fail under constraint tension.**  
   “Power first”, fixed rule weights, and feature piles create locally reasonable but globally bad boards. Priority ordering that is right near a BGA is wrong near analog.

3. **80/20 is the honest product model.**  
   Most nets are electrically straightforward busywork; a minority need expert judgment. Tools that thrash for hours on the hard 20% while leaving the easy 80% incomplete destroy trust.

4. **Verification & honesty > pretty incomplete copper.**  
   Open nets beat shorts. DRC-clean partials that engineers can finish beat opaque “optimized” messes.

5. **Benchmarks that matter:** completion rate, via economy, wall time on real boards — not cherry-picked demos.

## Mapping → physicsRouter

| DeepPCB idea | Our implementation |
|--------------|-------------------|
| Resource allocation first | Capacity mesh + PathFinder history + cut preflight (`capacity_mesh`, `global_router`, `graph_theory`) |
| Don’t seal the board with early multipin | **80/20 staged route**: 2-pin → 3–6 pin → heavy multipin → recovery (`hybrid_route._deeppcb_eighty_twenty_route`) |
| Easy 80% must complete | Residual 2-pin at **0.10 / 0.08 mm** after parallel wave; `microbench_segments.py` gates local_rc / 2pin |
| Avoid static power-first on dense boards | Large boards skip classic hybrid power/critical/matrix/general order |
| Fill the machine (cores / GPU / RAM) | OpenMP multi-layer A* + outline paint; OpenCL (Apple + Windows NVIDIA ICD); parallel 2-pin waves; RAM-scaled expansions |
| Open > short | Manufacturing gate + full-net commit + no soft illegal fallbacks |
| Intermediate speed for iteration | Segment microbenches; native progress window on `route`; full pad DRC on commit |
| Diagnostics for trust | `route_diagnostics` JSON/MD on every golden-eval |
| Via economy as quality | Golden efficiency metrics (length/via vs human when complete) |

### Measured 80/20 (mppc v1.3)

| Stage | Full golden (Win) | Empty-board microbench |
|-------|-------------------:|------------------------:|
| routine_2pin | ~11/20 | **16/20** with 0.10 residual |
| mid_3to6 | 46/59 | (analog-only: 12/16) |
| heavy_multipin | 1/6 | — |
| Overall grade | **D 39.41** (59/85) | N/A (segment only) |

## What we deliberately do *not* claim

- Full autonomous replacement of layout engineers on HEP-class boards.  
- RL training on proprietary corpora (we are topological + capacity + free-angle, not DeepPCB’s cloud RL).  
- That grade A on mppc is automatic — multipin GND/power still need pours and stronger negotiation.

## Grade roadmap (aligned with their 80/20)

1. **Maximize the easy 80%** (2-pin + short multipin) to 100% legal — residual fine grid shipped; GPIO escape next.  
2. **Budget remaining time** for heavy multipin with capacity reservation + conflict rip-up.  
3. **Pours / zones** for return paths after legal stubs.  
4. **Human-in-the-loop** for residual critical nets — `route --nets …` selective re-route (web UI removed).

Full lever list: [SCORE_ROADMAP.md](SCORE_ROADMAP.md).

## Reproduce staged path

```bash
# Fast checks
PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment 2pin
PYTHONPATH=src:native/build python scripts/microbench_segments.py --segment analog

# Full route (native progress window; --no-ui for headless)
physics-router route \
  --pcb examples/mppc-interface/mppcInterface_v1.3.kicad_pcb \
  --config examples/mppc-interface/placement_config.yaml \
  --pipeline capacity --effort 0.55
# Notes should include: hybrid: DeepPCB-style 80/20 staged route
```