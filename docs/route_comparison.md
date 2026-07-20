# Route comparison: TopoR vs FreeRouting

**TL;DR:** Generated table from `physics-router compare-routes`. Re-run with a FreeRouting SES for a real baseline. How-to: [CLI.md](CLI.md#freerouting) · [../examples/demo/FREEROUTING.md](../examples/demo/FREEROUTING.md).

```bash
physics-router compare-routes \
  --topor examples/demo/topor_route.json \
  --ses path/to/board.ses \
  --out comparison.json --md docs/route_comparison.md
```

---

| Metric | TopoR | FreeRouting | Δ (TopoR − FR) | Winner |
|--------|------:|------------:|---------------:|:------:|

_FreeRouting baseline not available in this snapshot._

| total_length_mm | 180.24 | — | — | — |
| via_count | 0 | — | — | — |
| segments | 12 | — | — | — |
| clearance_violations | 12 | — | — | — |

- No freerouting result in repo sample. Run FreeRouting on exported DSN and re-run compare with `--ses` or `--baseline-json`.

## Sources

- TopoR sample: `examples/demo/topor_route.json`
