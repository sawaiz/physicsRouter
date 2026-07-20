# Capacity-mesh autorouting

**TL;DR:** Before drawing copper, build a hierarchical **capacity mesh**, path nets on it, assign layers per section, then run free-angle detail. The mesh never paints tracks — it only answers *where is room* and *which layer*. Authoritative build is **C++** (`capacity_mesh.cpp`). Ideas from MIT [tscircuit capacity-autorouter](https://github.com/tscircuit/tscircuit-autorouter), not a TypeScript dependency.

| | |
|---|---|
| Python API | `capacity_mesh.py`, `route_pipeline.py` |
| C++ | `native/src/capacity_mesh.cpp` |
| CLI | `physics-router route --pipeline capacity --effort 0.55` |
| Related | [ARCHITECTURE_ROUTER.md](ARCHITECTURE_ROUTER.md) · [../native/README.md](../native/README.md) |

---

## Pipeline

```text
pin_access → topology + capacity_mesh → global_sections
         → hybrid / free-angle copper → manufacturing_gate
```

```python
from physics_router.route_pipeline import run_capacity_pipeline, RoutePipelineSolver

result = run_capacity_pipeline(board, config, rules, effort=0.6)

solver = RoutePipelineSolver(board, config, rules, effort=0.55)
while solver.step():
    print(solver.stage_name)
copper = solver.result
```

---

## Ideas adopted vs not

| tscircuit idea | Here |
|----------------|------|
| Hierarchical capacity mesh | `capacity_mesh` (C++ preferred) |
| Tuned capacity / depth from pitch | `tuned_node_capacity`, auto depth |
| Mesh A* with occupancy + history | `path_through_mesh` |
| Step-based pipeline | `RoutePipelineSolver` |
| Pin-access first | `pin_access.py` (+ BGA denser sampling) |
| Section layer assignment | `global_router` + C++ `plan_capacity_for_nets` |
| Fail-loud manufacturing gate | `manufacturing_gate` stage |

**Not copied:** TS high-density intra-node solvers, React cosmos UI, SimpleRouteJson intermediate, soft “success” on failed solves.

---

## C++ core (default on)

| API | Role |
|-----|------|
| `build_capacity_mesh` | Hierarchical cells over board + pads |
| `path_through_mesh` | A* on mesh adjacency |
| `plan_capacity_for_nets` | Section layers + preferred layer order |
| depth / effort knobs | Auto depth when `capacity_depth = -1` |

Inside `route_board` when `RouteConfig.enable_capacity_mesh` is true:

1. Build mesh over bounds + anchors  
2. Ensure topology edges (Prim MST if missing)  
3. Assign each edge a copper layer under occupancy + history  
4. Reorder preferred layers by section majority  
5. Detailed free-angle + full-net commit (unchanged ExactMap rules)

```text
enable_capacity_mesh = true
capacity_effort      = 0.55   # 0..1
capacity_depth       = -1     # -1 = auto
```

Python `build_capacity_mesh` prefers C++; falls back only if `pr_native` is missing.

---

## References

- [tscircuit capacity-autorouter](https://github.com/tscircuit/tscircuit-autorouter)  
- [Hypergraph autorouting](https://blog.autorouting.com/p/hypergraph-autorouting)  
- [DESIGN.md](../DESIGN.md) · [ARCHITECTURE_ROUTER.md](ARCHITECTURE_ROUTER.md)
