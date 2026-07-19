# physicsRouter

`physicsRouter` is a research PCB autorouter for KiCad. It combines a required
C++ free-angle geometry core with Python policy, graph planning, KiCad I/O,
validation and an interactive control plane.

The goal is not a route that merely looks connected. A successful result must
contain physically reachable pads, explicit vias at every layer transition,
complete multipin nets, zero native hard violations, and zero copper errors
after KiCad applies and refills the board.

> **Status:** native v1.9.1 fixes layer-blind connectivity, forbids via-in-pad,
> and adds
> board-wide PathFinder-style negotiated congestion with conflict-directed
> rip-up, but the
> HALO-90 stress board is not fully autorouted yet. Correctly open nets are
> preferable to illegal copper, but they are not a finished route.

## What failed on HALO-90

The pre-v1.8 router was run for 1,288 seconds on the real four-layer HALO PCB:

| Round | Strategy | KiCad copper violations | Vias | Unrouted nets |
|---|---:|---:|---:|---:|
| 1 | hybrid | 489 | 0 | 4 |
| 2 | native | 588 | 0 | 4 |
| 3 | native fine | 588 | 0 | 4 |

The selected route contained 68 inner-layer segments and no via objects even
though 251 of the board's 256 pads are SMD. KiCad found real cross-net shorts
while the in-process audit reported zero shorts and the final API summary reset
the known violation count to zero. The run also exceeded its 1,200-second
deadline because the deadline was checked only between native calls. The
binding now releases the GIL, but C++ search still needs an internal deadline.

The failure was structural, not a grid-resolution problem:

1. SMD anchors were treated as reachable on every preferred layer.
2. Component-body rectangles replaced real rotated, layer-specific pad copper.
3. XY segment generation was mistaken for multilayer electrical connectivity.
4. Layer choice and via placement were deferred to an emergency fallback.
5. Sequential CPX routing greedily consumed corridors without global
   negotiation.
6. Native DRC, KiCad DRC, scoring and API serialization described different
   physical objects.
7. A stale native module could be loaded without proving it matched the source
   commit.

The full diagnosis, publications and measured production-layout baseline are in
[docs/AUTOROUTER_FAILURE_ANALYSIS.md](docs/AUTOROUTER_FAILURE_ANALYSIS.md).

## Current routing architecture

| Phase | Implementation |
|---|---|
| **Exact board model** | Real pad centers, rotations, custom copper primitives, copper layers, arc Edge.Cuts, fab rules and through-via constraints |
| **Net hypergraph** | One multi-terminal object per net instead of unrelated two-pin requests |
| **Topology candidates** | Crossing-aware spanning trees and layout-aware alternatives |
| **Conflict graph** | Projected crossings become graph edges; DSATUR proposes copper layers |
| **Atomic C++ routing** | A net commits only when every anchor is physically connected |
| **Explicit pin access** | F.Cu/B.Cu SMD escapes and layer transitions emit real vias |
| **Native obstacles** | Oriented, net-owned pads painted only on their physical copper layers |
| **Bounded variants** | Whole priority/matrix buckets rebuilt in deterministic orders |
| **Negotiated congestion** | Signal nets may temporarily share capacity; present and historical costs move later candidates away from persistent conflicts |
| **Conflict-directed rip-up** | Exact native DRC markers form a net-conflict graph; deterministic independent sets are legalized and only victims are retried |
| **Organic power areas** | Native zone boundaries; KiCad remains fill/thermal authority |
| **Validation** | Embedded multilayer connectivity graph, native DRC, then official KiCad DRC |

```text
KiCad board + rules
        |
        v
pad/layer model -> pin-access graph -> net hypergraph/topology candidates
                                         |
                              conflict graph + layer/via plan
                                         |
                              negotiated global routing
                                         |
                         atomic track/via geometry transactions
                                         |
                    native DRC <-> local rip-up/search-and-repair
                                         |
                         zones/refill -> KiCad DRC -> score
```

The remaining algorithmic bottleneck is dense CPX bundle routing. v1.9 now
performs three bounded PathFinder rounds, accumulates historical cost on
overused cells and exact DRC markers, legalizes a maximal independent set of
the conflict graph, and repairs only its victims. It never returns temporary
overlap as committed copper and cannot regress the legal input baseline.

Current v1.9.1 HALO checkpoint: 12/23 nets, including CPX-1 and CPX-2, as 941
segments and 271.8 mm total track. It has zero native hard violations, zero
via/pad overlaps, and zero generated-copper KiCad errors. Eleven nets remain
atomically open and KiCad reports 168 unconnected items, so this is a legal
partial artifact rather than a finished board. The previous 42-via/GND-area
snapshot is intentionally superseded: the stricter audit found 33 via/pad
violations in it. The current legal selection uses no vias or areas, which is
honest but underuses the four-layer board; legal offset escape-via planning is
therefore a completion blocker.

## Definition of working

A candidate can be selected as “best” only when:

1. Every required pad belongs to the correct net's multilayer connected
   component.
2. Rejected nets leave no partial copper.
3. Every layer transition has a legal via and valid span.
4. Native pad/track/via/outline/connectivity DRC reports zero hard violations.
5. Applied and refilled KiCad copper reports zero copper DRC errors.
6. Viewer, score, route artifact and API expose identical metrics.
7. The run records source commit, native version/build flags, rules, seed and
   wall time.
8. Routing is deterministic for a seed, responsive, cancellable and bounded by
   a hard deadline.

Length, via count and visual smoothness are optimized only after these
invariants hold.

## Benchmarks

- **Synthetic:** all nets connected, native DRC zero, KiCad DRC zero.
- **HALO-90:** 90 locked LEDs, ten 19-pin CPX nets, 256 pads and four copper
  layers; primary topology, escape, via and congestion stress test.
- **Released HALO copper:** human-routed topology/layer reference, not a
  geometry-copy target.
- **FreeRouting DSN/SES:** external GPL-3.0 baseline; code is not copied into
  this MIT project.
- **Adversarial fixtures:** rotated SMD pads, inaccessible layers, missing and
  dangling vias, track-through-pad, narrow channels, zones and Edge.Cuts.

Never publish a route metric without its copper artifact and KiCad report.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Required native C++17 router.
bash scripts/build_native.sh
python -c "from physics_router.native_bridge import info; print(info())"

# Uses third_party/halo-90 when the repository is present.
physics-router serve --host 127.0.0.1 --port 8765
# http://127.0.0.1:8765/

pytest
python scripts/ci_regression.py
```

### CLI

```bash
physics-router import-nets --pcb board.kicad_pcb --project-dir . -o placement_config.yaml
physics-router route --config placement_config.yaml --pcb board.kicad_pcb \
  --out route.json --out-pcb routed.kicad_pcb --drc
physics-router drc --pcb routed.kicad_pcb --out-dir drc_out
physics-router export-dsn --config placement_config.yaml -o board.dsn
```

## Repository map

| Path | Role |
|---|---|
| `native/` | C++ free-angle search, occupancy/exact maps, atomic nets, vias and areas |
| `src/physics_router/graph_theory.py` | Hypergraphs, crossing-aware trees, conflict graph and DSATUR |
| `src/physics_router/native_bridge.py` | Exact board/pad/layer translation into the native core |
| `src/physics_router/router.py` | Routing orchestration, embedded connectivity, DRC and KiCad output |
| `src/physics_router/continuous_improve.py` | Candidate loop, scoring, deadlines and KiCad oracle |
| `src/physics_router/kicad_tools.py` | KiCad DRC/ERC, renders and exports |
| `viewer/` / `src/physics_router/server.py` | Local UI, API and live progress |
| `examples/halo-90/` | HALO configuration and recorded experiments |

## Documentation

- [Detailed failure analysis and scientific basis](docs/AUTOROUTER_FAILURE_ANALYSIS.md)
- [Design decisions](DESIGN.md)
- [Topology-first architecture](docs/ARCHITECTURE_ROUTER.md)
- [TopoR product-model research](docs/TOPOR.md)
- [Hybrid routing](docs/HYBRID_ROUTING.md)
- [Research bibliography](RESEARCH.md)
- [JLCPCB rules](docs/JLCPCB_4LAYER.md)
- [Native core](native/README.md)

Routing-process images are versioned experiments, not fabrication proof. They
must be regenerated with the route artifact and matching DRC reports:

```bash
python scripts/render_routing_process.py --halo
```

## Requirements and license

- Python 3.10+
- CMake 3.16+ and a C++17 compiler
- KiCad 8+ / `kicad-cli` for authoritative real-board validation
- Optional OpenMP, OpenCL, Ngspice and OpenEMS/CSXCAD

MIT. HALO-90 is a separate project cloned under the gitignored `third_party/`
directory.
