# mppcInterface v1.3 benchmark

Pinned human-routed golden from [muonTelescope/mppcInterface](https://github.com/muonTelescope/mppcInterface) @ **`580c61d`**.

| File | Role |
|------|------|
| `mppcInterface_v1.3.kicad_pcb` | 4-layer complete human route |
| `mppcInterface_v1.3.kicad_pro` | Project rules |
| `placement_config.yaml` | Net weights / physics |
| `manifest.yaml` | golden-eval entry |

Full report: **[docs/MPPC_BENCHMARK.md](../../docs/MPPC_BENCHMARK.md)**

```bash
python scripts/run_mppc_benchmark.py
physics-router golden-eval --manifest examples/mppc-interface/manifest.yaml
```
