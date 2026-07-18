# JLCPCB 4-layer design rules (DRC / ERC floors)

physicsRouter defaults multilayer work to **JLCPCB 4-layer FR-4** production
floors so autoroutes and DRC checks match fab capability.

Source: [JLCPCB PCB capabilities](https://jlcpcb.com/capabilities/pcb-capabilities).

## Profiles

| Profile | Track / space | Via (pad/drill) | Edge copper | Use when |
|---------|---------------|-----------------|-------------|----------|
| **4layer_recommended** (default) | ≥0.15 mm | 0.6 / 0.3 mm | ≥0.3 mm | Cheap reliable 4L |
| **4layer_capability** | ≥0.09 mm | 0.45 / 0.2 mm | ≥0.2 mm | Dense layout, absolute fab min |

Both profiles:

- **No blind/buried vias** (unsupported at JLCPCB)
- **No microvias**
- Through-hole vias only
- 1.6 mm thickness, 1 oz outer / 0.5 oz inner (typical)
- Stack: `F.Cu` / `In1.Cu` / `In2.Cu` / `B.Cu` — signals outer, planes inner

## Other DRC-related floors (recommended)

| Item | Value |
|------|-------|
| Via → track | ≥0.2 mm |
| PTH → track | ≥0.35 mm recommended (abs 0.28) |
| Via hole–hole | ≥0.2 mm (capability) |
| Pad hole–hole | ≥0.45 mm |
| Solder mask bridge | ≥0.10 mm (1 oz green) |
| Silk → pad | ≥0.15 mm |
| Silk line / text | ≥0.15 mm / ≥1.0 mm height |

## ERC policy

ERC remains schematic-side (`kicad-cli sch erc`). Router-side expectations:

- Power / ground use dedicated net classes (wider copper, plane layers)
- No reliance on microvia or blind/buried connectivity
- Unconnected pins reported via router unrouted list + KiCad DRC unconnected

## API

```python
from physics_router.design_rules import (
    jlcpcb_4layer_design_rules,
    load_design_rules,
    apply_manufacturer_floors,
)

rules = jlcpcb_4layer_design_rules()                 # recommended
rules = jlcpcb_4layer_design_rules(aggressive=True)  # capability
rules = load_design_rules(pcb_path, manufacturer="JLCPCB")
```

`load_design_rules` merges KiCad project numbers, then **raises** any floor that
is looser than JLCPCB (keeps stricter project values).

## Router defaults

- TopoR / hybrid / improve: clearance default = JLC min clearance  
- Grid default ≈ max(0.15, min track)  
- Native C++ free-angle path for geometry search; Python only for polish / hybrid planning  
