# FreeRouting baseline

1. Export DSN from physics-router:
   ```bash
   physics-router export-dsn --config examples/halo-90/placement_config.yaml \
     --pcb third_party/halo-90/pcb/halo-90.kicad_pcb -o compare/board.dsn
   ```

2. Run FreeRouting (GUI or CLI jar):
   ```bash
   java -jar freerouting.jar -de board.dsn -do board.ses -mp 100
   ```
   Download: https://github.com/freerouting/freerouting

3. Compare metrics:
   ```bash
   physics-router compare-routes \
     --topor compare/topor_route.json \
     --dsn compare/board.dsn \
     --ses compare/board.ses \
     --out compare/comparison.json
   ```

If FreeRouting is not installed, compare still works with TopoR-only results and
documents the baseline procedure for CI/manual runs.
