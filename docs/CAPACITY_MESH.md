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

## C++ core (authoritative)

Implemented in `native/src/capacity_mesh.cpp` and always run inside
``route_board`` when ``RouteConfig.enable_capacity_mesh`` is true (default):

| C++ API | Role |
|---------|------|
| `build_capacity_mesh` | Hierarchical quadtree capacity cells |
| `path_through_mesh` | A\* on mesh adjacency |
| `plan_capacity_for_nets` | Negotiate section layers + preferred layer order |
| `tuned_node_capacity` / `calculate_optimal_capacity_depth` | Capacity depth auto-tune |

Before sequential free-angle geometrization, native planning:

1. Builds a mesh over board bounds + pad anchors
2. Ensures each net has topology edges (Prim MST if missing)
3. Assigns each topology edge a copper layer under occupancy + history
4. Reorders ``preferred_layers`` by assigned section majority

Python `capacity_mesh.build_capacity_mesh` **prefers the C++ build** and only
falls back to pure Python if `pr_native` is unavailable.

RouteConfig knobs:

```text
enable_capacity_mesh = true
capacity_effort      = 0.55   # 0..1
capacity_depth       = -1     # -1 = auto
```

The mesh never draws copper. Exact free-angle geometry, ExactMap DRC, and
full-net commit remain in `pr_native` detailed search. The mesh only answers:
*where is capacity left, which layer should this section use?*

## References

- [tscircuit capacity-autorouter README](https://github.com/tscircuit/tscircuit-autorouter)
- [Hypergraph autorouting blog](https://blog.autorouting.com/p/hypergraph-autorouting)
- Local: [ARCHITECTURE_ROUTER.md](ARCHITECTURE_ROUTER.md), [DESIGN.md](../DESIGN.md)
