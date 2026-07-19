# KiCad plugin — physicsRouter

## Install

1. Install physicsRouter so `physics-router` is on your PATH (or set `PHYSICS_ROUTER_BIN`).
2. Copy `physics_router_action.py` into KiCad’s plugins folder:

| OS | Typical path |
|----|----------------|
| macOS | `~/Documents/KiCad/9.0/scripting/plugins/` |
| Linux | `~/.local/share/kicad/9.0/scripting/plugins/` |
| Windows | `%APPDATA%\kicad\9.0\scripting\plugins\` |

3. Restart pcbnew (or **Tools → External Plugins → Refresh Plugins**).
4. Open a board → **Tools → External Plugins → physicsRouter: Auto-route**.

## Environment

| Variable | Meaning |
|----------|---------|
| `PHYSICS_ROUTER_BIN` | Absolute path to `physics-router` CLI |
| `PHYSICS_ROUTER_TIMEOUT` | Max seconds (default 180) |
| `PR_KEEP_TMP` | Set to `1` to keep temp route files |

## Behaviour

Saves the current board, runs `physics-router smoke` (auto net import + capacity/hybrid pipeline), then reloads the routed `.kicad_pcb` into the editor.
