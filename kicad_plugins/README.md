# KiCad plugin

**TL;DR:** ActionPlugin saves the open board → runs `physics-router smoke` → reloads copper into pcbnew.

---

## Install

1. Install physicsRouter so `physics-router` is on your **PATH**, or set `PHYSICS_ROUTER_BIN` to the binary.
2. Copy `physics_router_action.py` into KiCad’s plugins folder:

| OS | Typical path |
|----|----------------|
| macOS | `~/Documents/KiCad/9.0/scripting/plugins/` |
| Linux | `~/.local/share/kicad/9.0/scripting/plugins/` |
| Windows | `%APPDATA%\kicad\9.0\scripting\plugins\` |

(Use your KiCad version number if not 9.0.)

3. Restart pcbnew, or **Tools → External Plugins → Refresh Plugins**.
4. Open a board → **Tools → External Plugins → physicsRouter: Auto-route**.

---

## What it does

1. Saves the current board to disk (or a temp file).  
2. Runs headless smoke (auto net import + capacity/hybrid route).  
3. Reloads the routed `.kicad_pcb` into the editor.  

For live progress while routing from the shell, use:

```bash
physics-router route --pcb board.kicad_pcb --out-pcb routed.kicad_pcb
```

(Native progress window; add `--no-ui` for headless.)

---

## Environment

| Variable | Meaning |
|----------|---------|
| `PHYSICS_ROUTER_BIN` | Absolute path to `physics-router` |
| `PHYSICS_ROUTER_TIMEOUT` | Max seconds (default 180) |
| `PR_KEEP_TMP` | `1` keeps temp route files for debugging |

---

## Docs

- [../docs/QUICKSTART.md](../docs/QUICKSTART.md)  
- [../docs/USER_GUIDE.md](../docs/USER_GUIDE.md)  
- [../docs/CLI.md](../docs/CLI.md) (`smoke` flags)
