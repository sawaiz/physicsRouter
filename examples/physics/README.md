# Muon3 / physics example

**TL;DR:** Routes the **muon telescope v10** layout from the sibling `../physics` repo (not the empty `muon3.kicad_pcb` shell). Config: `placement_config.yaml`.

---

## Board path

Server preset `physics` resolves to something like:

```text
../physics/reference_documentation/next_generation/nextgen_review/hardware/muon_telescope_v10/muon_telescope.kicad_pcb
```

If that path is missing, load the PCB explicitly:

```bash
physics-router smoke --pcb /absolute/path/to/muon_telescope.kicad_pcb \
  --config examples/physics/placement_config.yaml
```

---

## Run

```bash
physics-router route \
  --config examples/physics/placement_config.yaml \
  --pcb "$PHYSICS_PCB" \
  --out-json /tmp/physics_route.json --out-pcb /tmp/physics_routed.kicad_pcb

# Headless
physics-router route --no-ui \
  --config examples/physics/placement_config.yaml \
  --pcb "$PHYSICS_PCB" \
  --out-json /tmp/physics_route.json --out-pcb /tmp/physics_routed.kicad_pcb
```

---

## Config

`placement_config.yaml` holds project name, generous board envelope, net weights / function groups for the telescope layout. Re-import nets if the schematic changes:

```bash
physics-router import-nets \
  --pcb path/to/muon_telescope.kicad_pcb \
  --config examples/physics/placement_config.yaml \
  -o examples/physics/placement_config.yaml --override
```

---

## Docs

[../../docs/QUICKSTART.md](../../docs/QUICKSTART.md) · [../../docs/USER_GUIDE.md](../../docs/USER_GUIDE.md)
