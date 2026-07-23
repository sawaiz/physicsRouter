"""Native live-routing progress window (tkinter).

Opens a desktop window while autorouting: stage/net, progress bar, log, and a
2D canvas of pads + copper as segments are committed. No web server.

Usage::

    from physics_router.progress_ui import run_with_progress_window

    result = run_with_progress_window(
        title="physicsRouter · route",
        board=board,
        work=lambda cb: run_capacity_pipeline(..., progress_cb=cb),
    )
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# progress_cb contract used across hybrid / pipeline / native wrappers
ProgressFn = Callable[[int, int, str, str, dict], None]


@dataclass
class _Seg:
    x1: float
    y1: float
    x2: float
    y2: float
    layer: str = "F.Cu"
    net: str = ""
    width: float = 0.2


@dataclass
class _UiState:
    done: int = 0
    total: int = 1
    stage: str = "starting"
    status: str = ""
    log: list[str] = field(default_factory=list)
    segments: list[_Seg] = field(default_factory=list)
    finished: bool = False
    error: str | None = None
    result: Any = None
    t0: float = field(default_factory=time.time)


_LAYER_COLORS = {
    "F.Cu": "#ef4444",
    "B.Cu": "#3b82f6",
    "In1.Cu": "#22c55e",
    "In2.Cu": "#a855f7",
    "In3.Cu": "#f59e0b",
    "In4.Cu": "#14b8a6",
}


def _pad_points(board: Any) -> list[tuple[float, float, str]]:
    pts: list[tuple[float, float, str]] = []
    try:
        from physics_router.kicad_io import local_to_board
    except Exception:
        local_to_board = None  # type: ignore
    comps = getattr(board, "components", None) or {}
    for ref, comp in comps.items():
        for pad in getattr(comp, "pads", None) or []:
            try:
                if local_to_board is not None:
                    x, y = local_to_board(
                        float(comp.x_mm),
                        float(comp.y_mm),
                        float(getattr(comp, "rotation_deg", 0) or 0),
                        float(pad.get("x", 0) or 0),
                        float(pad.get("y", 0) or 0),
                    )
                else:
                    x = float(comp.x_mm) + float(pad.get("x", 0) or 0)
                    y = float(comp.y_mm) + float(pad.get("y", 0) or 0)
                pts.append((x, y, str(pad.get("net") or "")))
            except Exception:
                continue
    # Fallback: net anchors if components empty
    if not pts:
        nets = getattr(board, "nets", None) or {}
        for net, anchors in nets.items():
            for a in anchors or []:
                if isinstance(a, (list, tuple)) and len(a) >= 2:
                    try:
                        pts.append((float(a[0]), float(a[1]), str(net)))
                    except Exception:
                        pass
    return pts


def _bounds_from_board(board: Any, pads: list[tuple[float, float, str]]) -> tuple[float, float, float, float]:
    w = float(getattr(board, "width_mm", 0) or 0)
    h = float(getattr(board, "height_mm", 0) or 0)
    if pads:
        xs = [p[0] for p in pads]
        ys = [p[1] for p in pads]
        margin = 2.0
        return min(xs) - margin, max(xs) + margin, min(ys) - margin, max(ys) + margin
    if w > 0 and h > 0:
        return -w / 2, w / 2, -h / 2, h / 2
    return -50.0, 50.0, -50.0, 50.0


def run_with_progress_window(
    *,
    work: Callable[[ProgressFn | None], T],
    title: str = "physicsRouter · routing",
    board: Any = None,
    headless: bool = False,
) -> T:
    """Run ``work(progress_cb)`` while showing a native progress window.

    If tkinter is unavailable or ``headless=True``, runs without UI and still
    accepts a no-op progress callback.
    """
    if headless:
        return work(None)

    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return work(None)

    q: queue.Queue = queue.Queue()
    state = _UiState()
    pads = _pad_points(board) if board is not None else []
    xmin, xmax, ymin, ymax = _bounds_from_board(board, pads)

    def progress_cb(
        done: int, total: int, name: str, status: str, detail: dict | None = None
    ) -> None:
        detail = detail or {}
        segs_in: list[_Seg] = []
        raw_segs = detail.get("segments") or detail.get("segment_samples") or []
        # If "segments" is an int (count), ignore
        if isinstance(raw_segs, int):
            raw_segs = detail.get("segment_samples") or []
        for s in raw_segs:
            if not isinstance(s, dict):
                continue
            try:
                segs_in.append(
                    _Seg(
                        float(s.get("x1", s.get("x_1", 0))),
                        float(s.get("y1", s.get("y_1", 0))),
                        float(s.get("x2", s.get("x_2", 0))),
                        float(s.get("y2", s.get("y_2", 0))),
                        str(s.get("layer") or "F.Cu"),
                        str(s.get("net") or name or ""),
                        float(s.get("width_mm") or s.get("width") or 0.2),
                    )
                )
            except Exception:
                continue
        q.put(
            {
                "done": int(done),
                "total": max(1, int(total)),
                "stage": str(name or ""),
                "status": str(status or ""),
                "detail": detail,
                "segments": segs_in,
                "replace_segments": bool(segs_in and detail.get("segment_samples")),
                "log": f"[{done}/{total}] {name} · {status}",
            }
        )

    def worker() -> None:
        try:
            state.result = work(progress_cb)
            # If result has segments, push final copper
            res = state.result
            segs = getattr(res, "segments", None) or []
            final_segs: list[_Seg] = []
            for s in segs:
                try:
                    final_segs.append(
                        _Seg(
                            float(s.x1),
                            float(s.y1),
                            float(s.x2),
                            float(s.y2),
                            str(getattr(s, "layer", "F.Cu") or "F.Cu"),
                            str(getattr(s, "net", "") or ""),
                            float(getattr(s, "width_mm", 0.2) or 0.2),
                        )
                    )
                except Exception:
                    continue
            q.put(
                {
                    "done": 1,
                    "total": 1,
                    "stage": "done",
                    "status": "complete",
                    "segments": final_segs,
                    "replace_segments": True,
                    "log": f"finished · {len(final_segs)} segments · "
                    f"{getattr(res, 'via_count', 0)} vias · "
                    f"unrouted={len(getattr(res, 'unrouted_nets', []) or [])}",
                }
            )
        except Exception as exc:  # noqa: BLE001
            state.error = str(exc)
            q.put({"error": str(exc), "log": f"ERROR: {exc}"})
        finally:
            state.finished = True
            q.put({"finished": True})

    th = threading.Thread(target=worker, daemon=True)
    th.start()

    root = tk.Tk()
    root.title(title)
    root.geometry("960x640")
    root.minsize(640, 420)

    # Header
    hdr = ttk.Frame(root, padding=8)
    hdr.pack(fill=tk.X)
    title_lbl = ttk.Label(hdr, text=title, font=("TkDefaultFont", 12, "bold"))
    title_lbl.pack(anchor=tk.W)
    stage_var = tk.StringVar(value="starting…")
    stage_lbl = ttk.Label(hdr, textvariable=stage_var)
    stage_lbl.pack(anchor=tk.W)
    pvar = tk.DoubleVar(value=0.0)
    pbar = ttk.Progressbar(hdr, maximum=100.0, variable=pvar, length=400)
    pbar.pack(fill=tk.X, pady=4)
    stats_var = tk.StringVar(value="0 / 0")
    ttk.Label(hdr, textvariable=stats_var).pack(anchor=tk.W)

    body = ttk.Panedwindow(root, orient=tk.HORIZONTAL)
    body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

    # Canvas
    canvas_frame = ttk.Frame(body)
    body.add(canvas_frame, weight=3)
    canvas = tk.Canvas(canvas_frame, bg="#0f172a", highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)

    # Log
    log_frame = ttk.Frame(body)
    body.add(log_frame, weight=1)
    ttk.Label(log_frame, text="Log").pack(anchor=tk.W)
    log_txt = tk.Text(log_frame, height=20, width=36, wrap=tk.WORD, bg="#1e293b", fg="#e2e8f0")
    log_txt.pack(fill=tk.BOTH, expand=True)
    log_txt.configure(state=tk.DISABLED)

    btn_row = ttk.Frame(root, padding=8)
    btn_row.pack(fill=tk.X)
    close_btn = ttk.Button(btn_row, text="Close (when finished)", state=tk.DISABLED)
    close_btn.pack(side=tk.RIGHT)

    segs_drawn: list[_Seg] = []

    def world_to_canvas(x: float, y: float) -> tuple[float, float]:
        cw = max(canvas.winfo_width(), 10)
        ch = max(canvas.winfo_height(), 10)
        # invert Y for screen
        nx = (x - xmin) / max(1e-9, xmax - xmin)
        ny = (y - ymin) / max(1e-9, ymax - ymin)
        return 8 + nx * (cw - 16), 8 + (1.0 - ny) * (ch - 16)

    def redraw() -> None:
        canvas.delete("all")
        # board box
        x0, y0 = world_to_canvas(xmin, ymin)
        x1, y1 = world_to_canvas(xmax, ymax)
        canvas.create_rectangle(x0, y0, x1, y1, outline="#334155", width=1)
        # pads
        for px, py, net in pads:
            cx, cy = world_to_canvas(px, py)
            canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill="#94a3b8", outline="")
        # copper
        for s in segs_drawn:
            c1 = world_to_canvas(s.x1, s.y1)
            c2 = world_to_canvas(s.x2, s.y2)
            col = _LAYER_COLORS.get(s.layer, "#f8fafc")
            lw = max(1.0, min(4.0, s.width * 8.0))
            canvas.create_line(c1[0], c1[1], c2[0], c2[1], fill=col, width=lw, capstyle=tk.ROUND)

    def append_log(line: str) -> None:
        log_txt.configure(state=tk.NORMAL)
        log_txt.insert(tk.END, line + "\n")
        log_txt.see(tk.END)
        log_txt.configure(state=tk.DISABLED)

    def on_close() -> None:
        if state.finished:
            root.destroy()

    close_btn.configure(command=on_close)
    root.protocol("WM_DELETE_WINDOW", on_close)

    def poll() -> None:
        try:
            while True:
                msg = q.get_nowait()
                if msg.get("log"):
                    append_log(str(msg["log"]))
                if "done" in msg and "total" in msg:
                    d, t = int(msg["done"]), max(1, int(msg["total"]))
                    pvar.set(100.0 * d / t)
                    stats_var.set(f"{d} / {t} · {time.time() - state.t0:.1f}s")
                    stage_var.set(f"{msg.get('stage', '')} · {msg.get('status', '')}")
                if msg.get("replace_segments"):
                    segs_drawn.clear()
                for s in msg.get("segments") or []:
                    segs_drawn.append(s)
                if msg.get("segments") or msg.get("replace_segments"):
                    redraw()
                if msg.get("error"):
                    stage_var.set(f"error: {msg['error']}")
                if msg.get("finished"):
                    close_btn.configure(state=tk.NORMAL)
                    stage_var.set(stage_var.get() + " — done (Close to exit)")
        except queue.Empty:
            pass
        if not state.finished or not q.empty():
            root.after(50, poll)
        else:
            # keep UI open until user closes
            root.after(200, poll)

    root.after(30, redraw)
    root.after(50, poll)
    root.mainloop()
    th.join(timeout=1.0)

    if state.error:
        raise RuntimeError(state.error)
    return state.result  # type: ignore[return-value]
