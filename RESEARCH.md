# Research survey: PCB placement & routing algorithms

**TL;DR:** Literature that informs physicsRouter — classical EDA, topological routers, ML/RL, TopoR-adjacent patents/papers. Implementation map: [DESIGN.md](DESIGN.md) · [docs/ARCHITECTURE_ROUTER.md](docs/ARCHITECTURE_ROUTER.md). Not a product user guide (see [docs/QUICKSTART.md](docs/QUICKSTART.md)).

Companion to the main [README](README.md).

---

## 1. Problem formulation

**Placement** assigns \((x, y, \theta)\) to each footprint subject to non-overlap, board outline, fixed connectors, and multi-objective quality (wirelength, congestion, thermal, EMI, DFM).

**Routing** realizes nets as copper paths (tracks + vias) under clearance, layer, width, impedance, and differential-pair constraints.

PCB placement differs from pure IC standard-cell placement: arbitrary package shapes, mixed through-hole/SMD, fixed mechanicals, few layers, and strong **electrical** objectives (power loops, return paths) beyond HPWL.

---

## 2. Classical placement algorithms

### 2.1 Simulated annealing (SA)

Kirkpatrick et al. (1983) introduced SA for combinatorial optimization. PCB literature widely uses SA for component placement: random move/rotate/swap accepted with temperature schedule \(e^{-\Delta E/T}\).

- **Strengths:** Robust baseline; handles discrete rotations; easy multi-objective energy.
- **Weaknesses:** Slow on large boards; needs good cooling schedule.
- **Evidence:** Holtz/Merrill-style SA is a strong PCB baseline in RL papers; Vassallo & Bajada (DATE 2024) compare RL policies against SA on post-routing wirelength.

**physicsRouter:** multi-candidate SA with weighted energy (wirelength, loop area, critical nets, overlap, density, thermal, EMI).

### 2.2 Genetic algorithms (GA) & evolutionary methods

Jain & Gea (1996) and later works model placement (or pick-and-place sequencing as TSP) with GAs. Self-organizing GAs and multi-objective GA (MIT theses) co-optimize thermal and wirelength.

- **Strengths:** Population diversity; multi-objective Pareto fronts.
- **Weaknesses:** Many evaluations; parameter-sensitive.

### 2.3 Force-directed & analytical methods

Hall’s quadratic placement (1970) and force-directed layouts treat nets as springs. Thermal placement uses heat-conduction analogies (repulsion of hot parts). IC analytical placers (ePlace, APlace, RePlAce) minimize wirelength + density via nonlinear optimization; PCB adaptations must handle large mixed-size modules.

### 2.4 Partitioning / min-cut / spectral methods

Recursive bipartition and Laplacian eigenvector initialization (spectral clustering) produce seed floorplans. **NS-Place** (Cheng, Ho, Holtz, arXiv:2210.14259) initializes with Laplacian eigenmaps, then optimizes a **net-separation** objective (max-margin separators between net convex hulls, SVM-like) plus wirelength/density, legalizes with MILP, and validates with FreeRouting—reporting large reductions in DRVs, unrouted nets, and vias on 14 real PCBs.

### 2.5 Particle swarm & other metaheuristics

PSO has been applied to thermal PCB layout; generally less standard than SA/GA for production EDA.

---

## 3. Classical routing algorithms

### 3.1 Maze routing (Lee algorithm)

Lee (1961): BFS wave propagation on a grid—**optimal shortest path** if one exists. Slow \(O(MN)\) and memory-heavy. Variants: Hadlock, Soukup, A\* with clearance costs.

### 3.2 Line-probe / line-search (Hightower, Mikami–Tabuchi)

Faster than maze routing for sparse boards; weaker completeness guarantees.

### 3.3 Channel / switchbox / area routers

Structured for IC; less natural for free-form PCB boards.

### 3.4 Shape-based / gridless routers

Commercial Specctra-class and **FreeRouting** operate off-grid with shape push-aside. Better density than pure grid maze; DSN/SES interchange standard.

### 3.5 Topological / rubberband routing

- **Dayan (PhD, UCSC 1997):** *Rubberband based topological router*—interconnect as rubber-band sketch (RBS); topology first, geometry second.
- **Dai, Dayan, Staepelaere (DAC 1991):** SURF system—hierarchical topological rubberband sketches.
- **TopoR (Eremex):** commercial free-angle **isotropic** topological autorouter — see full product reference **[docs/TOPOR.md](docs/TOPOR.md)** (images + manuals + binary catalog).
- **Open-source Toporouter (Blake / gEDA, GSoC 2008):** Dayan-inspired; adapted toward KiCad.

**Key idea:** free angles + continuous deformation under clearance reduce vias and crosstalk vs strict 45°/90° preferred directions.

#### Commercial TopoR (Eremex) — public product model

Primary sources: [product](https://www.eremex.com/products/topor/), [features](https://www.eremex.com/products/topor/features/), [autorouting](https://www.eremex.com/products/topor/competitiveadvantages/autorouting/), [design time](https://www.eremex.com/products/topor/competitiveadvantages/pcbdesigntime/), [high-speed](https://www.eremex.com/products/topor/competitiveadvantages/highspeedpcbs/), [downloads](https://www.eremex.com/downloads/) (login), 6.1 [user manual PDF](https://www.eremex.com/support/documentation/461750.pdf).

| Concept | What Eremex documents |
|---------|----------------------|
| **Isotropic routing** | No preferred 90°/45° directions; arbitrary angles + arcs; denser packing, less parallelism → lower crosstalk |
| **Topology → geometry** | Path is a flexible topological relationship first; exact wire shape is recalculated (components/vias can move on a live board) |
| **Instant route + optimize** | 100% connectivity quickly (even with temporary rule violations), then multiobjective optimization |
| **Multi-variant parallel** | Several alternative topologies optimized at once (length / vias / density); user selects survivors |
| **BGA / flex / 1L** | Specialized BGA; flex with via-free bend regions; single-layer boards where shape-based tools leave unroutes |
| **High-speed** | Length limits, differential pairs, bus matching; trapezoid tuning (50 nm *computational* precision) |
| **Interchange** | DSN/SES, P-CAD, PADS, Eagle, Expedition HKP, native `.fst` / `.fsx` / `.fsb` |

Public download catalog (as of 2026 site scrape): **TopoR 7.0.18508** Lite/Trial x86 & x64 (2017), User guide 7.0 (2018). Login required; unauthenticated download URLs 404.

Cached figures and PDFs: [`docs/images/topor/`](docs/images/topor/README.md).

**physicsRouter:** free-angle clearance-aware router (LOS → corner detours → A\* → rubberband), net priority by weights, multi-layer vias, copper paint; optional C++/OpenCL core. Not a clone of TopoR’s proprietary engine — conceptual mapping in [docs/TOPOR.md §11](docs/TOPOR.md).

### 3.6 Escape routing & differential pairs

BGA escape and differential-pair co-routing (e.g. Lin et al., ASP-DAC 2021 unified PCB router) are specialized subproblems; industrial tools still often semi-manual for HDI.

---

## 4. Machine learning & reinforcement learning

### 4.1 IC placement RL (transferable ideas)

Mirhoseini, Goldie et al. (Nature 2021; arXiv:2004.10746): sequential macro placement as RL with graph CNN; pretrain across chips; force-directed standard cells after macros. Open-sourced as Circuit Training / AlphaChip. **Caveat:** later methodological controversy (CACm reevaluation)—still highly influential for **learnable placement policies** and proxy rewards (wirelength, congestion).

### 4.2 PCB placement RL

| Work | Idea | Takeaway for us |
|------|------|-----------------|
| Crocker (MIT MEng 2021) | DRL placement with **routing-based** physical verification | Prefer post-route metrics over pure HPWL |
| Vassallo & Bajada (DATE 2024); RL_PCB | Cellular/component agents, adaptive rewards, post-route WL | Multi-agent local moves; SA baseline |
| Lim et al. (arXiv:2602.23540, 2026) | Component-centric layout; DQN / A2C / SA | Fix large ICs; place passives near pins |
| Archives des Sciences (2024) | Graph vs feature state encoding for DRL module layout | Graph netlist features help |

### 4.3 PCB routing ML / RL

| Work | Idea | Takeaway |
|------|------|----------|
| He (Iowa State PhD 2024) | **DRL + MCTS** routing; pad-focused polygon partitioning; **PCBench** dataset | MCTS for long-horizon routes; open benchmarks |
| Xiang et al. (2024) | Multi-agent RL local-observation autorouting | Parallel net agents |
| Unet-Astar (IEEE Access ~2023) | CNN guides A\* for unified PCB routing | Learning to cost/heuristic maps |
| DreamerV3+FR (2026) | World-model RL + **FreeRouting** environment | Integrate RL with existing autorouters |
| TRouter | Thermal-driven routing via attention networks | Physics-aware routing objectives |
| DeepPCB (industry) | RL-based commercial autorouter | Scale + tool integrations (KiCad, Altium, …) |

### 4.4 Hybrid & modern directions

- **U-Net / CNN cost maps** + classical A\* (learned guidance, classical legality).
- **GNN** on netlist hypergraphs for affinity / clustering before place.
- **Diffusion** placers (IC: “Chip Placement with Diffusion”, arXiv:2407.12282)—simultaneous placement; PCB analogue underexplored.
- **LLM benchmarks:** PCB-Bench (ICLR 2026)—placement/routing *reasoning*, not geometric P&R.
- **Safe RL / CMDPs** (surveys 2024–2025): hard constraints (DRC, overlap) as formal constraints—not only soft penalties.

---

## 5. Physics-aware objectives (beyond wirelength)

Research and practice agree HPWL alone is insufficient for PCBs:

| Objective | Typical method |
|-----------|----------------|
| Power loop / EMI | Geometric loop area; partial inductance; OpenEMS / 2.5D EM |
| SI / impedance | Stackup rules; length matching; EM simulation |
| Thermal | Force-directed heat models; FEM |
| Congestion / vias | Net-separation (NS-Place); post-route FreeRouting metrics |
| SPICE behavior | Rail parasitics; ngspice on estimated R/L |

**physicsRouter:** multi-objective score + Ngspice/OpenEMS proxies; full OpenEMS export for shortlists.

---

## 6. Methodologies that make routing *easier* (tests & policies)

Beyond raw path search, research and practice emphasize **pre-route policy** that reduces failures:

| Methodology | What it does | Citations / practice |
|-------------|--------------|----------------------|
| **KiCad DRC floors** | Never route thinner/tighter than min clearance/track/via | KiCad Board Setup; fab capability |
| **Stackup-aware layer assign** | 4L SIG–GND–PWR–SIG; critical nets next to reference plane | Multilayer SI; impedance control |
| **Via minimization / layer assignment** | Same-layer first; charge vias; global layer assign | Congestion-constrained layer assignment literature |
| **Net ordering** | Power/GND → clocks/high-speed → pairs → general | Industrial autorouters |
| **Escape-then-area** | Fan out dense packages before area-route | BGA escape; pad-focused routing (He 2024) |
| **H/V preferred per layer** | Alternate preferred directions (free-angle LOS still OK) | Specctra-style packing |
| **3D line exploration** | Radar scan + layer-transition cost | *Sci. Reports* 2026 geometric multi-layer routing |
| **MAPF / MLV-CBS** | Multi-agent path finding for min layers/vias | MLV-CBS PCB routing ~2025 |
| **MCTS multi-layer actions** | Expand through layers as actions | He PhD DRL-MCTS |
| **Pre-route density test** | pins/cm² → suggest more layers | Practice + NS-Place congestion mindset |
| **Pair co-route** | Diff pairs / I2C matched length, same layer | ASP-DAC unified PCB router |
| **Physics pre-checks** | Loop area, EMI, ngspice before final copper | This project; OpenEMS |

**physicsRouter:** `design_rules` load from KiCad, `pre-route` report, ordered nets, `multilayer_route` with DRC widths/vias + rubberband cleanup, pair hints, IR/loop-L/return-path/matrix-match scores, OpenEMS JSON + **KiCad STEP** (tracks/pads/mask/silk) for accurate 3D EM.

## 7. Recommended algorithm stack (synthesis)

1. **Net labeling / criticality** — netclasses + designer weights.  
2. **Import KiCad stackup + DRC** — clearance/width/via floors; copper layers.  
3. **Pre-route tests** — density, escape hints, via budget, layer advice.  
4. **Floorplan seed** — regions / spectral / fixed connectors.  
5. **Global place** — multi-objective SA (→ future RL).  
6. **Legalization** — non-overlap (MILP in literature).  
7. **Validate place** — physics proxies; expensive sim on shortlists.  
8. **Route** — TopoR free-angle on stackup layers; through-via policy from KiCad.  
9. **Close loop** — post-route metrics → re-place.

---

## 8. Full bibliography (selected)

### Classical & topological

1. C. Y. Lee, “An algorithm for path connections and its applications,” *IRE Trans. Electronic Computers*, 1961.  
2. S. Kirkpatrick, C. D. Gelatt, M. P. Vecchi, “Optimization by simulated annealing,” *Science*, 1983.  
3. K. M. Hall, “An r-dimensional quadratic placement algorithm,” *Management Science*, 1970.  
4. T. Dayan, *Rubberband based topological router*, PhD thesis, UC Santa Cruz, 1997.  
5. W. W.-M. Dai, T. Dayan, D. Staepelaere, “Topological routing in SURF: generating a rubber-band sketch,” DAC 1991.  
6. [TopoR](https://en.wikipedia.org/wiki/TopoR) (Eremex) — topological free-angle commercial router; product docs + image cache: [docs/TOPOR.md](docs/TOPOR.md).  
6a. Eremex, TopoR product / features / competitive advantages — https://www.eremex.com/products/topor/  
6b. Eremex, *TopoR 6.1 User Manual* — https://www.eremex.com/support/documentation/461750.pdf  
6c. Eremex, TopoR datasheet — https://www.eremex.com/products/topor/features/445337.pdf  
7. A. Blake, gEDA Toporouter (GSoC 2008) — open topological router.  
8. FreeRouting — open shape-based Specctra-compatible autorouter.  

### PCB placement (analytic / metaheuristic)

9. S. Jain, H. C. Gea, “PCB Layout Design Using a Genetic Algorithm,” *J. Electronic Packaging*, 1996.  
10. F. S. Ismail et al., self-organizing GA for component placement, *J. Intell. Manuf.*, 2012.  
11. T. Badriyah et al., GA + Lee routing for PCB optimization, 2016.  
12. A. Alexandridis et al., PSO for PCB thermal design, *Integrated Computer-Aided Engineering*, 2017.  
13. C.-K. Cheng, C.-T. Ho, C. Holtz, “Net Separation-Oriented Printed Circuit Board Placement via Margin Maximization,” arXiv:2210.14259, 2022 (NS-Place).  
14. T.-C. Lin et al., “A unified printed circuit board routing algorithm…,” ASP-DAC 2021.  

### RL / ML placement & routing

15. A. Mirhoseini, A. Goldie et al., “A graph placement methodology for fast chip design,” *Nature*, 2021; arXiv:2004.10746; Circuit Training / AlphaChip.  
16. P. Crocker, *Physically Constrained PCB Placement Using Deep Reinforcement Learning*, MIT, 2021.  
17. L. Vassallo, J. Bajada, “Learning Circuit Placement Techniques Through Reinforcement Learning with Adaptive Rewards,” DATE 2024; [RL_PCB](https://github.com/LukeVassallo/RL_PCB).  
18. Y. He, *Towards automated PCB routing: Leveraging machine learning and heuristic techniques*, PhD dissertation, Iowa State University, 2024 (DRL-MCTS, PCBench).  
19. Q. Xiang et al., multi-agent RL PCB routing, IEEE, 2024.  
20. Unet-Astar: deep learning + A\* for unified PCB routing, *IEEE Access*, ~2023.  
21. K. L. Lim et al., “Component Centric Placement Using Deep Reinforcement Learning,” arXiv:2602.23540, 2026.  
22. DreamerV3+FR: world-model RL + FreeRouting, *Expert Systems with Applications*, 2026.  
23. TRouter: thermal-driven PCB routing via attention networks.  
24. PCB-Bench: Benchmarking LLMs for PCB Placement and Routing, ICLR 2026.  
25. Minimal-Layer Via / multi-agent PCB routing (MAPF-CBS family), ~2025.  
26. 3D line-exploration geometric routing for multi-layer PCBs, *Scientific Reports*, 2026.  
27. KiCad documentation — Board Setup: net classes, constraints, stackup.  

### Topology-first patents & continuous-space (design influence — FTO required for products)

30. US 7,937,681 / US 2006/0242614 — automated PCB routing: topology graph, relaxed then tightened constraints, geometry feedback.  
31. US 7,017,137 — topological global routing for package interconnect; guide points to detail.  
32. US 8,510,703 — PCB routing-space representation; via placement as global plan.  
33. US 2023/0306177 — topological/geometric curvilinear routes; hybrid free-angle + preferred directions.  
34. 3D LineExplore — continuous multilayer geometric routing, *Sci. Reports* 2026.  
35. Unconstrained via minimization for topological multilayer routing (IEEE classic).  
36. Circular-frame package routing, arXiv:2105.07892.  
37. Multi-agent minimal-layer via routing (CBS-style), ~2025.  
38. Negotiated congestion / parallel FPGA routing, arXiv:2407.00009 (principle transfer).  
39. Obstacle-aware any-direction length matching, arXiv:2407.19195.  
40. PCBWorld engine-grounded benchmark, arXiv:2607.05915 (2026).  
41. PCB-Dreamer world-model RL around FreeRouting, *ESWA* 2026.  
42. FreeRouting — open Specctra-class baseline ([GitHub](https://github.com/freerouting/freerouting)); **GPL-3 study only** if product is not GPL.  
43. Eremex publications index — *Isotropic PCB Routing*, BGA routing, topological CAD concepts — https://www.eremex.com/support/publications/  
44. Project architecture: [docs/ARCHITECTURE_ROUTER.md](docs/ARCHITECTURE_ROUTER.md).  

### Datasets & tools

25. [PCBench](https://github.com/PCBench/PCBench) — KiCad routing dataset + RL env.  
26. [Open Schematics](https://huggingface.co/datasets/bshada/open-schematics) — large schematic/PCB corpus.  
27. [DATASETS.md](DATASETS.md) — project corpus guide.  

### Physics simulation

28. openEMS / CSXCAD — open FDTD EM solver ([docs](https://docs.openems.de/)).  
29. Ngspice — open circuit simulator.  

---

### Multilayer / via / geometric (additional)

28. Congestion-constrained layer assignment for via minimization in global routing (classic VLSI/PCB theme).  
29. Minimal-Layer Via CBS / multi-agent PCB routing (MAPF-style), 2025.  
30. 3D line-exploration geometric routing for multi-layer PCBs, *Scientific Reports*, 2026.  
31. KiCad documentation — Board Setup: net classes, constraints, stackup.  

## 9. Community signals (X / practice)

Industry and builders emphasize:

- **RL trial-and-error at scale** for commercial autorouting (DeepPCB: 40k+ routings; DAC demos).
- **Classical grid/shape routers still win** on hard boards when ML tools fail—e.g. custom KiCad grid autorouters written in days when FreeRouting/DeepPCB struggled on dense memory interfaces.
- **Physics-aware commercial tools** (Quilter and similar) and agentic flows (Flux, LLM + KiCad plugins) for prototypes; high-speed/safety boards still need expert SI/EMI review.
- Dedicated layout engines separate from general agents for speed and less rework during routing.

These align with a hybrid design: **strong classical + topological core**, **physics objectives**, **ML/RL where data and rewards are clear**.
