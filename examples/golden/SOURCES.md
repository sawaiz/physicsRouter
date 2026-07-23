# Golden board sources & licenses

Boards live under `third_party/golden/` (gitignored). Fetch:

```bash
bash scripts/fetch_golden_boards.sh
```

| ID | Upstream | License (typical) | Notes |
|----|----------|-------------------|-------|
| `simple_2net` | in-repo fixture | MIT (physicsRouter) | 2-net teaching board |
| **`mppc_v1.3`** | [muonTelescope/mppcInterface](https://github.com/muonTelescope/mppcInterface) @ **`580c61d`** | upstream | **Primary HEP golden** — 4L complete human route |
| `halo-90` | [openKolibri/halo-90](https://github.com/openKolibri/halo-90) | project license | Dense charlieplex stress |
| `vme_wren` | KiCad demos / [OHWR WREN](https://ohwr.org/projects/wren/) | CERN-OHL-W family | CERN White Rabbit event node class, 12-layer demo |
| `openipmc_hw` | [gitlab.com/openipmc/openipmc-hw](https://gitlab.com/openipmc/openipmc-hw) | open (see repo) | ATCA IPMC — HEP crate mgmt |
| `satnogs_comms` | Libre Space Foundation | CERN-OHL | Satellite COMMS, RF + digital |
| `pq9_devboard` | Libre Space Foundation | CERN-OHL | Cubesat PQ9 bus |
| `jetson_nano` | [antmicro/jetson-nano-baseboard](https://github.com/antmicro/jetson-nano-baseboard) | Apache-2.0 (check repo) | SBC carrier BGA escape |
| `jetson_agx_thor` | KiCad demos (Antmicro class) | KiCad demo / upstream | Extreme carrier |
| `ofm_illumination` / `openflexure_illum` | OpenFlexure / OFM | CERN-OHL-S | Science microscope LED |
| KiCad demos (`video`, `ecc83_*`, …) | [kicad demos](https://gitlab.com/kicad/code/kicad/-/tree/master/demos) | KiCad project | Curated variety |

**Not available as open KiCad:** PHENIX / sPHENIX front-end boards (use WREN + dense multipin as proxies).

Always record the git commit SHA when publishing benchmarks.
