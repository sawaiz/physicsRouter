# JLCPCB design rules (2 / 4 / 6 layer)

**TL;DR:** Use `4layer_recommended` for most boards; `4layer_capability` for dense (HALO default). Pick in viewer under **Fab profile**, or `load_design_rules(..., jlc_profile=...)`.

Source: [JLCPCB PCB capabilities](https://jlcpcb.com/capabilities/pcb-capabilities).

## Profiles (pick one)

| Profile id | Layers | Track / space | Via pad/drill | Edge copper | When to use |
|------------|--------|---------------|---------------|-------------|-------------|
| `2layer_recommended` | 2 | ≥0.20 mm | 0.6 / 0.3 | ≥0.3 mm | Cheap prototypes, simple PCBs |
| `2layer_capability` | 2 | ≥0.10 mm | 0.45 / 0.2 | ≥0.2 mm | Dense 2L (DFM risk / cost) |
| `4layer_recommended` | 4 | ≥0.15 mm | 0.6 / 0.3 | ≥0.3 mm | **Default** for most designs |
| `4layer_capability` | 4 | ≥0.09 mm | 0.45 / 0.2 (0.125 radial annulus) | ≥0.2 mm | BGA / dense multipin; HALO-90 default |
| `6layer_recommended` | 6 | ≥0.15 mm | 0.6 / 0.3 | ≥0.3 mm | HS / extra planes |
| `6layer_capability` | 6 | ≥0.09 mm | 0.45 / 0.2 | ≥0.2 mm | High density 6L |

### Shared limitations (all JLC profiles)

- **No blind / buried vias** (through-hole only)
- **No microvias** / HDI stacks
- Outer copper typically **1 oz**, inner **0.5 oz** (multi)
- Thickness default **1.6 mm**

## Suggestions by layer count

### 2-layer
- Put **GND pour on B.Cu** under signal clusters  
- Expect **more vias** for crossings; keep power short and wide  
- Dense LED matrices / BGA usually need **4L+**

### 4-layer
- Prefer **SIG / GND / PWR / SIG** (or SIG–GND–GND–SIG)  
- Route signals on outer layers; keep inners for return/power pours  
- Best cost / capability balance for mixed-signal wearables

### 6-layer
- Prefer solid reference next to high-speed nets  
- Through-hole vias still stub the full stack — length-match carefully  
- Use JLC impedance calculator for controlled-Z stacks  

## API

```python
from physics_router.design_rules import (
    jlcpcb_design_rules,
    jlcpcb_2layer_design_rules,
    jlcpcb_4layer_design_rules,
    jlcpcb_6layer_design_rules,
    list_jlcpcb_profiles,
    load_design_rules,
)

rules = jlcpcb_design_rules(layers=2)              # recommended 2L
rules = jlcpcb_design_rules(layers=6, aggressive=True)
rules = load_design_rules(pcb, jlc_profile="6layer_recommended")
print(list_jlcpcb_profiles())  # UI catalog
```

### HTTP

- `GET /api/snapshot` → `fab_profile`, `fab_profiles[]` (label, summary, suggestions, limitations)
- `POST /api/fab-profile` `{"profile": "2layer_recommended"}`  
- Route / Improve jobs accept `params.fab_profile`

## Router defaults

Clearance / grid default from the **selected profile** floors. Geometry search
stays on the **native C++** path.
