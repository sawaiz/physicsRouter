# HALO ring routing (halo.js style)

The HALO-90 earring is **not** a general Manhattan board. Its LED matrix is a
**charlieplexed ring**: ninety 0402 LEDs on a circle, ten CPX buses, four copper
layers. The reference generator
[`third_party/halo-90/pcb/halo.js`](../third_party/halo-90/pcb/halo.js) does not
use A\*; it **constructs** copper from polar geometry.

physicsRouter can use the same geometry model so autoroutes look and DRC like the
hand-authored HALO copper — not radial “star” shorts from free-angle search.

## What halo.js does

| Step | Geometry |
|------|----------|
| Place LEDs | Equal angles on radius ≈ 11 mm |
| Charlie map | For each LED slot, which CPX signal is on the “column” pad |
| Layer map | Each CPX signal → `(layer, track)` where **track** is a concentric ring index (0 = under LEDs / outermost, 3 = innermost) |
| Outer arcs | F.Cu arcs just outside the LED ring stitch “row” groups |
| Via rings | Breakout vias at `R−1`, signal vias at `R − (1 + track·pitch)` |
| Signal copper | **Radial spoke → concentric arc → radial spoke** on the assigned track |
| Full rings | Track 0 and max track get continuous 360° arcs |

Key constants (from halo.js):

- `radius = 11`, `numLed = 90`
- `innerViaSpacing = 0.55`
- `clearence = 0.128`, `traceWidth = 0.128`
- Layers: Front / In1.Cu / In2.Cu / Back (KiCad: F.Cu, In1.Cu, In2.Cu, B.Cu)

```
         track 0  (under LEDs / outer)
      ·  track 1
      ·  track 2
      ·  track 3  (inner)
           ·  via ring / MCU breakout
                ·  center (U1)
```

## Why free-angle alone fails

Isotropic A\* / LOS from LED pads to U1 draws **chords across the disk**. On a
dense 4-layer ring those chords cross other CPX nets on the same layer → KiCad
`shorting_items` / `tracks_crossing` even when native “exact” counts look
optimistic. Concentric tracks **cannot cross** on the same layer; crossings only
happen at intentional vias between layers.

## physicsRouter implementation

| Piece | Role |
|-------|------|
| `physics_router.halo_ring` | Detect ring, assign CPX→track, emit polar copper |
| `style="ring"` / auto on HALO | Entry from `clearance_aware_route` / TopoR / Improve |
| Native `route_point` polar try | Radial+arc candidate before detours/A\* when `ring_mode` |
| Checker | Native exact DRC + optional KiCad-cli oracle; ring notes for track pitch |

### Polar path (two pins, same net)

1. Measure center `(cx,cy)` = mean of LED positions (≈ origin on HALO).
2. Assigned track radius  
   `R_t = R_led − (1 + inner_via_pitch·(max_track+1) + track·2·clearance)`  
   (matches halo.js inner-signal formula; track 0 uses `R_led`).
3. Path: pin → radial to `R_t` → arc (polyline samples) to goal angle → radial to goal.
4. If path is blocked on the preferred layer, try via to another layer’s track for that net.

### Non-CPX nets

Power, GND, SW, UART, etc. still use normal clearance-aware / native free-angle
routing after CPX copper is painted as obstacles.

## Enabling

```python
from physics_router.halo_ring import halo_ring_route, detect_led_ring
result = halo_ring_route(board, config, clearance_mm=0.128, width_mm=0.128)
```

CLI / UI: route with style **ring**, or load HALO-90 (auto-detect ≥ 24 LEDs on a circle).

Improve loop prefers ring strategies first when a ring is detected.

## Checker / DRC

1. **Native** `drc_check` — segment/via clearance, shorts, outline.
2. **KiCad** `kicad-cli pcb drc` on exported copper with **real net codes** (not net 0).
3. Ring-aware notes: min pitch between different-net concentric arcs on same layer.

## Limitations

- Assumes roughly circular LED ring and CPX-* net names.
- MCU fanout is approximate (center breakout ring); fine pad-level escape may need a second pass.
- Exact via coordinates from halo.js are reconstructed from the same formulas, not parsed from the .js file at runtime.
