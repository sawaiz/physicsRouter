#!/usr/bin/env python3
"""KiCad pcbnew ActionPlugin — route current board with physicsRouter.

Install
-------
Copy or symlink this file (or the whole ``kicad_plugins/`` folder) into one of:

* macOS: ``~/Documents/KiCad/9.0/scripting/plugins/``
  (or the versioned path shown in **Preferences → Paths → Plugins**)
* Linux: ``~/.local/share/kicad/9.0/scripting/plugins/``
* Windows: ``%APPDATA%/kicad/9.0/scripting/plugins/``

Or set env ``KICAD_PLUGIN_PATH`` / use **Plugin and Content Manager**.

Requires the ``physics-router`` CLI on PATH (or set ``PHYSICS_ROUTER_BIN``).

Usage in pcbnew: **Tools → External Plugins → physicsRouter: Auto-route**
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import traceback
from pathlib import Path

try:
    import pcbnew
    import wx
except ImportError:  # allow import outside KiCad for packaging checks
    pcbnew = None  # type: ignore
    wx = None  # type: ignore


def _find_cli() -> str | None:
    env = os.environ.get("PHYSICS_ROUTER_BIN") or os.environ.get("PHYSICS_ROUTER")
    if env and Path(env).exists():
        return env
    which = shutil.which("physics-router")
    if which:
        return which
    # Common local venv next to this plugin / repo
    here = Path(__file__).resolve()
    for cand in (
        here.parents[1] / ".venv" / "bin" / "physics-router",
        here.parents[2] / ".venv" / "bin" / "physics-router",
        Path.home() / "physicsRouter" / ".venv" / "bin" / "physics-router",
    ):
        if cand.is_file():
            return str(cand)
    return None


def _notify(title: str, msg: str, error: bool = False) -> None:
    if wx is None:
        print(f"{title}: {msg}")
        return
    style = wx.OK | (wx.ICON_ERROR if error else wx.ICON_INFORMATION)
    wx.MessageBox(msg, title, style)


class PhysicsRouterPlugin(pcbnew.ActionPlugin if pcbnew else object):  # type: ignore[misc]
    def defaults(self) -> None:
        self.name = "physicsRouter: Auto-route"
        self.category = "Routing"
        self.description = (
            "Route the board with physicsRouter (zero-violation free-angle) "
            "and write copper back into the current PCB."
        )
        self.show_toolbar_button = True
        self.icon_file_name = ""  # optional PNG next to plugin

    def Run(self) -> None:
        if pcbnew is None:
            raise RuntimeError("pcbnew not available — run inside KiCad")
        board = pcbnew.GetBoard()
        if board is None:
            _notify("physicsRouter", "No board loaded.", error=True)
            return

        cli = _find_cli()
        if not cli:
            _notify(
                "physicsRouter",
                "physics-router CLI not found.\n"
                "Install the package and ensure `physics-router` is on PATH,\n"
                "or set PHYSICS_ROUTER_BIN to the binary.",
                error=True,
            )
            return

        # Save current board to a temp path so CLI can read it
        src_path = board.GetFileName()
        tmp = Path(tempfile.mkdtemp(prefix="pr_kicad_"))
        try:
            if src_path:
                pcb_in = Path(src_path)
                # Ensure disk has latest edits
                board.Save(str(pcb_in))
            else:
                pcb_in = tmp / "board.kicad_pcb"
                board.Save(str(pcb_in))

            out_pcb = tmp / "routed.kicad_pcb"
            out_json = tmp / "route.json"
            timeout = int(os.environ.get("PHYSICS_ROUTER_TIMEOUT", "180"))

            cmd = [
                cli,
                "route",
                "--pcb",
                str(pcb_in),
                "--out-pcb",
                str(out_pcb),
                "--out-json",
                str(out_json),
                "--pipeline",
                "auto",
                "--fail-on-unrouted",
                "--no-drc",  # DRC optional; user can run in KiCad
            ]
            # Config is optional — auto-import nets when omitted via smoke-style path
            # Use smoke if available for config-free boards
            smoke = [cli, "smoke", "--pcb", str(pcb_in), "--out-pcb", str(out_pcb),
                     "--out-json", str(out_json), "--timeout", str(timeout)]
            use_smoke = True
            try:
                proc = subprocess.run(
                    smoke if use_smoke else cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout + 30,
                )
            except subprocess.TimeoutExpired:
                _notify("physicsRouter", f"Timed out after {timeout}s.", error=True)
                return

            log = (proc.stdout or "") + "\n" + (proc.stderr or "")
            if proc.returncode != 0 and not out_pcb.is_file():
                _notify(
                    "physicsRouter failed",
                    f"exit {proc.returncode}\n\n{log[-1500:]}",
                    error=True,
                )
                return

            if not out_pcb.is_file():
                _notify("physicsRouter", "No output PCB written.\n" + log[-800:], error=True)
                return

            # Load routed copper into editor
            board.Load(str(out_pcb))
            # Prefer Revert-style refresh
            try:
                pcbnew.Refresh()
            except Exception:
                pass
            _notify(
                "physicsRouter",
                f"Route applied from {out_pcb.name}.\n"
                f"exit={proc.returncode}\n\n{log[-600:]}",
                error=proc.returncode != 0,
            )
        except Exception as exc:
            _notify("physicsRouter error", f"{exc}\n{traceback.format_exc()[-800:]}", error=True)
        finally:
            # Keep tmp for debugging if PR_KEEP_TMP=1
            if os.environ.get("PR_KEEP_TMP") != "1":
                shutil.rmtree(tmp, ignore_errors=True)


# KiCad discovers subclasses of ActionPlugin registered at import time
if pcbnew is not None:
    try:
        PhysicsRouterPlugin().register()
    except Exception:
        pass
