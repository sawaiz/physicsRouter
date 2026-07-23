# Viewer 3D assets

**TL;DR:** Optional GLB/STEP snapshots for tooling. Prefer `export-step` for 3D meshes.

| File | Contents |
|------|----------|
| `halo-90.glb` | Footprints, tracks, pads, zones, mask, silk |
| `physics.glb` / `custom.glb` | Other boards when exported |
| `*_routed_*.kicad_pcb` | Optional applied-route PCB snapshots |

```bash
physics-router export-step --pcb path.kicad_pcb -o out.step
```

The former web UI (`serve`) is removed; routing progress uses a native window via
`physics-router route`. See [../../docs/USER_GUIDE.md](../../docs/USER_GUIDE.md).
