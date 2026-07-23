# Route diagnostics — mppc_v1.3

_Generated 2026-07-23T14:40:56_

## Summary

- Completion: **0.4824** (41 ok · 44 open)
- Grade / score: **F** / 18.24
- Hard DRC: **0** · gate passed: False
- Copper: 318 segs · 94 vias · 3 areas · 805.055 mm
- Pipeline: `capacity_mesh+hybrid`

**Score formula:** score ≈ 100*completion − min(30, 5*missing_nets); length/via bonuses only when completion ≥ 99%

- Toward D (≥35): Need roughly completion ≥ 0.65 with 0 hard DRC
- Toward C (≥55): Need roughly completion ≥ 0.85 with 0 hard DRC
- Toward B (≥75): Need roughly completion ≥ 0.95 with 0 hard DRC
- Toward A (≥90): Need roughly completion ≥ 0.99 + efficiency

## Missing nets by category

- **analog_channel** (14): `CH0, CH1, CH2, CH3, CH4, CH5, CH6, CH7, DAC0, DAC1, DAC3, DAC4, DAC6, DAC7`
- **digital_bus** (6): `CLK, MISO, MOSI, SCLK, ~CS_{DAC}, ~CS_{FPGA}`
- **gpio** (5): `GPIO17, GPIO18, GPIO22, GPIO23, GPIO27`
- **led** (3): `LED-0, LED-1, LED-2`
- **local_rc** (10): `Net-(C11-Pad1), Net-(R12-Pad2), Net-(R16-Pad2), Net-(R20-Pad2), Net-(R24-Pad2), Net-(R28-Pad2), Net-(R3-Pad2), Net-(R32-Pad2), Net-(R36-Pad2), Net-(R8-Pad2)`
- **other** (2): `FPGA_{DONE}, ~FPGA_{RST}`
- **power_gnd** (4): `+3V3, +5V, GND, HV`

## Difficulties

### [high] incomplete_nets

44 nets open (48% complete)

- Raise net weights for missing power/bus nets in placement_config.yaml
- Route power with pours/zones after legal tracks (improve --physics-feedback)
- Prefer few-pin nets first within power class so GND does not starve last

### [high] power_gnd_open

Power/GND open: +3V3, +5V, GND, HV

- Human goldens use large pours (zones) for GND/power — AR under-uses areas
- Add dedicated power-plane / zone fill stage after multipin connectivity
- Ensure config lists +5V/+3V3/GND with net_class power/ground and high weight

### [high] ripup_exhausted

23 empty rip-up attempts (no legal alternate)

- PathFinder-style negotiated congestion on conflict clusters (enable for <70 nets)
- Increase rip-up budget and peer set for multipin nets
- Re-order: route short 2-pin locals before long buses that seal corridors

### [medium] corridor_hogging

8 completed nets >> human length (corridor hogging)

- Cap detour ratio vs Steiner/MST guide length during commit
- Charge shared capacity earlier so early nets cannot monopolize lanes
- Elastic regeometry / rubber-band after full legal set

### [medium] global_overflow

Global section overflow=51, mesh_overflow_nodes=3206

- Raise capacity_effort / mesh depth on dense boards
- Overflow-aware Steiner already on — feed occupancy into detail cost more strongly
- Split multipin nets into hierarchical sections (CBS / tree packing)

### [low] no_matrix_bucket

No matrix strategy nets (counts={'power': 6, 'general': 74, 'critical': 5}); dense multipin treated as general

- Classify multipin analog channels (CH*, DAC*) as matrix when pin count ≥ 3
- Use finer grid (0.15) for channel fanout like matrix phase

### [info] open_over_short_ok

Hard DRC = 0 with open nets (policy working)

- Keep manufacturing gate: never trade shorts for completion
- Grade is dominated by completion ratio — fix open nets, not length

## Recommended actions

1. Raise net weights for missing power/bus nets in placement_config.yaml
2. Route power with pours/zones after legal tracks (improve --physics-feedback)
3. Prefer few-pin nets first within power class so GND does not starve last
4. Human goldens use large pours (zones) for GND/power — AR under-uses areas
5. Add dedicated power-plane / zone fill stage after multipin connectivity
6. Ensure config lists +5V/+3V3/GND with net_class power/ground and high weight
7. PathFinder-style negotiated congestion on conflict clusters (enable for <70 nets)
8. Increase rip-up budget and peer set for multipin nets
9. Re-order: route short 2-pin locals before long buses that seal corridors
10. Cap detour ratio vs Steiner/MST guide length during commit
11. Charge shared capacity earlier so early nets cannot monopolize lanes
12. Elastic regeometry / rubber-band after full legal set

## Hybrid phases

- `power`: +135 segs +42 vias unrouted=3
- `critical`: +135 segs +42 vias unrouted=5
- `general`: +318 segs +94 vias unrouted=36

## Rip-up

- Empty attempts: **23** / 23

- `FPGA_{DONE}`: 3 attempts
- `GPIO17`: 3 attempts
- `GPIO18`: 3 attempts
- `GPIO22`: 3 attempts
- `Net-(C11-Pad1)`: 3 attempts
- `Net-(R3-Pad2)`: 3 attempts
- `GPIO23`: 1 attempts
- `Net-(R12-Pad2)`: 1 attempts
- `Net-(C12-Pad1)`: 1 attempts
- `Net-(Q1-Pad1)`: 1 attempts
- `DAC5`: 1 attempts

## Corridor bloat (AR ≫ human length)

| Net | AR mm | Human mm | Ratio |
|-----|------:|---------:|------:|
| Net-(C51-Pad2) | 7.72 | 1.75 | 4.41 |
| Net-(C56-Pad2) | 7.72 | 1.75 | 4.41 |
| Net-(C61-Pad2) | 7.72 | 1.75 | 4.41 |
| +5V-A | 302.44 | 84.7 | 3.57 |
| Net-(R2-Pad2) | 10.01 | 3.44 | 2.91 |
| /5V-BOOST | 24.62 | 10.7 | 2.3 |
| -5V | 189.18 | 94.18 | 2.01 |
| Net-(Q1-Pad1) | 10.86 | 5.66 | 1.92 |

## Capacity / pin-access

- final_overflow: 51
- mesh_overflow_nodes: 3206
- planned_vias: 363
- via_profile: via_0p6
- shared_escape savings_ratio: 0.1913

## Router notes (tail)

```
ripup(empty): FPGA_{DONE} vs Net-(Q1-Pad1),Net-(R3-Pad2),Net-(R4-Pad1) (attempt 2)
ripup(empty): FPGA_{DONE} vs ~CS_{HV},DAC2,DAC5 (attempt 3)
ripup(empty): GPIO17 vs Net-(C24-Pad1),Net-(C26-Pad2),Net-(C29-Pad1) (attempt 1)
ripup(empty): GPIO17 vs Net-(C31-Pad2),Net-(C34-Pad1),Net-(C36-Pad2) (attempt 2)
ripup(empty): GPIO17 vs Net-(C39-Pad1),Net-(C41-Pad2),Net-(C44-Pad1) (attempt 3)
ripup(empty): GPIO18 vs Net-(C46-Pad2),Net-(C49-Pad1),Net-(C51-Pad2) (attempt 1)
ripup(empty): GPIO18 vs Net-(C54-Pad1),Net-(C56-Pad2),Net-(C59-Pad1) (attempt 2)
ripup(empty): GPIO18 vs Net-(C61-Pad2),Net-(C65-Pad2),Net-(C68-Pad1) (attempt 3)
ripup(empty): GPIO22 vs Net-(D4-Pad2),Net-(C11-Pad1),Net-(C27-Pad1) (attempt 1)
ripup(empty): GPIO22 vs Net-(C32-Pad1),Net-(C42-Pad1),Net-(C47-Pad1) (attempt 2)
ripup(empty): GPIO22 vs Net-(C52-Pad1),Net-(C57-Pad1),Net-(C62-Pad1) (attempt 3)
ripup(empty): GPIO23 vs /5V-BOOST (attempt 1)
ripup(empty): Net-(R12-Pad2) vs Net-(C37-Pad1) (attempt 1)
ripup(empty): Net-(C12-Pad1) vs Net-(R2-Pad2) (attempt 1)
ripup(empty): Net-(C11-Pad1) vs Net-(C21-Pad1),Net-(L5-Pad1),Net-(L6-Pad2) (attempt 1)
ripup(empty): Net-(C11-Pad1) vs Net-(Q1-Pad1),Net-(R3-Pad2),Net-(R4-Pad1) (attempt 2)
ripup(empty): Net-(C11-Pad1) vs ~CS_{HV},DAC2,DAC5 (attempt 3)
ripup(empty): Net-(Q1-Pad1) vs Net-(C21-Pad1),Net-(L5-Pad1),Net-(L6-Pad2) (attempt 1)
ripup(empty): Net-(R3-Pad2) vs Net-(Q1-Pad1),Net-(C24-Pad1),Net-(C26-Pad2) (attempt 1)
ripup(empty): Net-(R3-Pad2) vs Net-(C29-Pad1),Net-(C31-Pad2),Net-(C34-Pad1) (attempt 2)
ripup(empty): Net-(R3-Pad2) vs Net-(C36-Pad2),Net-(C39-Pad1),Net-(C41-Pad2) (attempt 3)
ripup(empty): DAC5 vs Net-(R4-Pad1),~CS_{HV},DAC2 (attempt 1)
grade F (0/100) · 0 soft viol · 94 vias · 36 unrouted · 805.1 mm
hybrid: general phase nets=74 cl=0.200 grid=0.2
hybrid phase general: +318 segs +94 vias +3 areas unrouted=36
router_drc: 0 violation(s) (0 short, 0 spacing) @ clearance 0.15mm
ROUTE FAILED manufacturing gate: 41/85 complete nets, 0 native DRC violation(s)
route graph: components=41 cycles=4 crossings=0
grade F (0/100) · 0 soft viol · 94 vias · 44 unrouted · 805.1 mm
ROUTE FAILED manufacturing gate: 41/85 complete nets, 0 native DRC violation(s)
```

