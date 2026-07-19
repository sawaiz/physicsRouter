# Capacity-mesh autorouting (tscircuit-inspired)

physicsRouter incorporates **design ideas** from the MIT-licensed
[`@tscircuit/capacity-autorouter`](https://github.com/tscircuit/tscircuit-autorouter)
without vendoring the TypeScript package.

## Ideas we adopted

| tscircuit concept | physicsRouter module |
|-------------------|----------------------|
| Hierarchical **capacity mesh** (subdivide until leaf capacity ~ target) | `capacity_mesh.py` |
| Tuned capacity from via/track pitch | `tuned_node_capacity`, `calculate_optimal_capacity_depth` |
| **Capacity pathing** A\* on mesh cells with occupancy + history | `path_through_mesh` |
| Multi-stage **pipeline** with explicit `step()` | `route_pipeline.py` → `RoutePipelineSolver` |
| Pin-access / assignable vias first | `pin_access.py` (already) |
| Global section layer assignment | `global_router.py` (mesh-biased costs) |
| Fail-loud manufacturing gate | pipeline stage `manufacturing_gate` |
| Effort / capacity depth knobs | `effort`, `capacity_depth` |

## Ideas we did **not** copy wholesale

- Full high-density intra-node TS portfolio solvers
- React visualization cosmos playground
- SimpleRouteJson intermediate (we keep `BoardModel` / `RouteResult`)
- Soft fallbacks that mark failed solves as success (their AGENTS.md policy matches our zero-violation gate)

## Pipeline stages

```text
pin_access → topology + capacity_mesh → global_sections
         → hybrid detailed copper → manufacturing_gate
```

```python
from physics_router.route_pipeline import run_capacity_pipeline, RoutePipelineSolver

# One-shot
result = run_capacity_pipeline(board, config, rules, effort=0.6)

# Step API (UI / jobs)
solver = RoutePipelineSolver(board, config, rules, effort=0.55)
while solver.step():
    print(solver.stage_name)
copper = solver.result
```

## Relationship to C++ detail

The mesh never draws copper. Exact free-angle geometry, ExactMap DRC, and
full-net commit remain in `pr_native` + `router.py`. The mesh only answers:
*where is capacity left, which layer should this section use, which vias are legal?*

## References

- [tscircuit capacity-autorouter README](https://github.com/tscircuit/tscircuit-autorouter)
- [Hypergraph autorouting blog](https://blog.autorouting.com/p/hypergraph-autorouting)
- Local: [ARCHITECTURE_ROUTER.md](ARCHITECTURE_ROUTER.md), [DESIGN.md](../DESIGN.md)
