# Viewer 3D assets

KiCad exports for the three.js control plane:

| File | Contents |
|------|----------|
| `halo-90.glb` | Binary glTF: footprint **STEP** models, tracks, pads, zones, inner copper, **soldermask**, **silkscreen**, board body |
| `halo-90_full.step` | Same geometry as CAD STEP (optional) |

Regenerate (requires KiCad `kicad-cli`):

```bash
physics-router serve   # auto-exports if missing when HALO PCB is loaded
# or from the UI: Simulate → Rebuild board 3D (GLB)
# or CLI:
```

```bash
"/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli" pcb export glb -f \
  -o viewer/assets/halo-90.glb \
  --subst-models \
  --include-tracks --include-pads --include-zones --include-inner-copper \
  --include-silkscreen --include-soldermask \
  third_party/halo-90/pcb/halo-90.kicad_pcb
```

Component models come from `third_party/halo-90/pcb/components/**/*.stp` (and `.step`) referenced by footprints (`${KIPRJMOD}/…`).
