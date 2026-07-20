# Viewer 3D assets

**TL;DR:** GLB/STEP files the web UI loads for 3D. Auto-export when a board is loaded in `serve`, or rebuild from the **3D** step.

| File | Contents |
|------|----------|
| `halo-90.glb` | Footprints, tracks, pads, zones, mask, silk |
| `physics.glb` / `custom.glb` | Other preset / imported boards when exported |
| `*_routed_*.kicad_pcb` | Optional applied-route PCB snapshots |

```bash
physics-router serve   # exports 3D when a real PCB is loaded
# UI: 3D step → Rebuild
# or:
physics-router export-step --pcb path.kicad_pcb -o out.step
```

User docs: [../../docs/USER_GUIDE.md](../../docs/USER_GUIDE.md).
