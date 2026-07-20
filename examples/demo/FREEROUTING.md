# FreeRouting baseline

**TL;DR:** Export DSN → FreeRouting → optional SES import → compare metrics. physicsRouter does not ship the FreeRouting JAR.

---

## Steps

```bash
# 1. DSN (real pad XY + class rules)
physics-router export-dsn \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb \
  --config examples/halo-90/placement_config.yaml \
  -o compare/board.dsn

# 2. FreeRouting (install separately)
# https://github.com/freerouting/freerouting
java -jar freerouting.jar -de board.dsn -do board.ses -mp 100

# 3. SES → KiCad copper (optional)
physics-router import-ses \
  --ses compare/board.ses \
  --pcb third_party/halo-90/pcb/halo-90.kicad_pcb \
  -o compare/board_fr.kicad_pcb

# 4. Compare to physicsRouter TopoR JSON
physics-router compare-routes \
  --topor compare/topor_route.json \
  --ses compare/board.ses \
  --out compare/comparison.json
```

Sample DSN in this folder: `board.dsn`.

---

## Docs

[../../docs/CLI.md](../../docs/CLI.md#freerouting) · [../../docs/USER_GUIDE.md](../../docs/USER_GUIDE.md)
