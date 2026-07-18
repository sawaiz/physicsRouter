#!/usr/bin/env python3
"""Headless 2D board render matching the control-plane canvas transforms.

Used to compare physicsRouter visualization against kicad-cli SVG/PNG plots.

  python scripts/render_viewer_2d.py
  python scripts/render_viewer_2d.py --pcb path.kicad_pcb -o out.png
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw

from physics_router.config_io import load_config
from physics_router.kicad_io import (
    load_board_from_kicad_pcb,
    local_to_board,
    pad_corners_board,
)
from physics_router.viewer_export import board_to_viewer_dict

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PCB = ROOT / "third_party/halo-90/pcb/halo-90.kicad_pcb"
DEFAULT_CFG = ROOT / "examples/halo-90/placement_config.yaml"
OUT_DIR = ROOT / "docs" / "images" / "viewer_compare"

# Match viewer/index.html
VIEW_FLIP_Y = True
VIEW_FLIP_BOARD = False

COLORS = {
    "F.Cu": (200, 52, 52, 200),
    "B.Cu": (58, 107, 200, 180),
    "In1.Cu": (76, 166, 76, 150),
    "In2.Cu": (200, 122, 40, 150),
    "F.SilkS": (232, 208, 96, 220),
    "F.Fab": (160, 160, 160, 160),
    "F.CrtYd": (208, 64, 208, 100),
    "Edge.Cuts": (208, 216, 224, 240),
    "bg": (10, 16, 32, 255),
    "substrate": (20, 55, 40, 230),
}


def view_xy(x: float, y: float) -> tuple[float, float]:
    vx, vy = x, y
    if VIEW_FLIP_Y:
        vy = -y
    if VIEW_FLIP_BOARD:
        vx = -vx
    return vx, vy


def view_rot_canvas_deg(deg: float) -> float:
    """Canvas rotation degrees (HTML/PIL positive = CW with Y-down)."""
    r = float(deg or 0)
    if VIEW_FLIP_Y and not VIEW_FLIP_BOARD:
        return r  # board CCW → CW on screen after Y-flip
    if VIEW_FLIP_Y and VIEW_FLIP_BOARD:
        return -r
    return -r


def hex_rgba(name: str, alpha: int | None = None) -> tuple[int, int, int, int]:
    c = COLORS.get(name, (180, 180, 180, 200))
    if alpha is not None:
        return (c[0], c[1], c[2], alpha)
    return c


class Canvas:
    def __init__(self, size: int = 1600, margin: float = 0.06):
        self.size = size
        self.img = Image.new("RGBA", (size, size), COLORS["bg"])
        self.draw = ImageDraw.Draw(self.img, "RGBA")
        self.ox = size / 2
        self.oy = size / 2
        self.scale = 1.0
        self.margin = margin

    def fit(self, points: list[tuple[float, float]]) -> None:
        if not points:
            self.scale = 40.0
            return
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)
        bw = max(maxx - minx, 1.0)
        bh = max(maxy - miny, 1.0)
        usable = self.size * (1 - 2 * self.margin)
        self.scale = 0.94 * min(usable / bw, usable / bh)
        cx = (minx + maxx) / 2
        cy = (miny + maxy) / 2
        self.ox = self.size / 2 - self.scale * cx
        self.oy = self.size / 2 + self.scale * cy  # Y-up view → image Y-down

    def tx(self, x: float, y: float) -> tuple[float, float]:
        vx, vy = view_xy(x, y)
        return self.ox + self.scale * vx, self.oy - self.scale * vy

    def board_to_local_img(
        self, lx: float, ly: float, fx: float, fy: float, frot_deg: float
    ) -> tuple[float, float]:
        """Footprint local mm → image pixels (pcbnew-accurate place + view)."""
        bx, by = local_to_board(fx, fy, frot_deg, lx, ly)
        return self.tx(bx, by)


def collect_bounds(board_dict: dict) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for c in board_dict.get("components") or []:
        vx, vy = view_xy(c["x"], c["y"])
        pts.append((vx, vy))
        # pad extents
        for g in c.get("graphics") or []:
            if g.get("kind") == "pad":
                for dx, dy in ((g["w"] / 2, g["h"] / 2), (-g["w"] / 2, -g["h"] / 2)):
                    lx = g["x"] + dx
                    ly = g["y"] + dy
                    bx, by = local_to_board(c["x"], c["y"], c["rot"], lx, ly)
                    pts.append(view_xy(bx, by))
    for g in board_dict.get("outline") or []:
        if g.get("kind") == "circle":
            r = g.get("r") or 12
            for a in range(0, 360, 30):
                rad = math.radians(a)
                pts.append(view_xy(g["cx"] + r * math.cos(rad), g["cy"] + r * math.sin(rad)))
        elif g.get("kind") == "poly":
            for p in g.get("pts") or []:
                pts.append(view_xy(p[0], p[1]))
        elif g.get("kind") == "line":
            pts.append(view_xy(g["x1"], g["y1"]))
            pts.append(view_xy(g["x2"], g["y2"]))
    return pts


def draw_outline(cv: Canvas, outline: list[dict]) -> None:
    d = cv.draw
    col = hex_rgba("Edge.Cuts")
    # Substrate fill: prefer origin-centered Edge.Cuts circle (main disk), not hook tip.
    r_disk = 0.0
    cx0 = cy0 = 0.0
    for g in outline:
        if g.get("kind") != "circle":
            continue
        r = float(g.get("r") or 0)
        cx, cy = float(g.get("cx") or 0), float(g.get("cy") or 0)
        if (cx * cx + cy * cy) ** 0.5 < 0.5 and r > r_disk:
            r_disk = r
            cx0, cy0 = cx, cy
    if r_disk < 5:
        # Fallback: median of outline points near 12 mm
        rs = []
        for g in outline:
            if g.get("kind") == "poly" and g.get("pts"):
                for p in g["pts"]:
                    r = math.hypot(p[0], p[1])
                    if 8.0 < r < 12.8:
                        rs.append(r)
        if rs:
            rs.sort()
            r_disk = rs[len(rs) // 2]
    # Substrate disk fill only (no stroke) — outline stroke comes from arc polys so
    # the hook teardrop is not closed by a full circle through the gap.
    has_polys = any(g.get("kind") == "poly" and g.get("pts") for g in outline)
    if r_disk > 5:
        c = cv.tx(cx0, cy0)
        r_px = r_disk * cv.scale
        d.ellipse(
            [c[0] - r_px, c[1] - r_px, c[0] + r_px, c[1] + r_px],
            fill=COLORS["substrate"],
            outline=None,
        )

    for g in outline:
        w = max(2, int((g.get("width") or 0.15) * cv.scale * 2))
        if g["kind"] == "line":
            a, b = cv.tx(g["x1"], g["y1"]), cv.tx(g["x2"], g["y2"])
            d.line([a, b], fill=col, width=w)
        elif g["kind"] == "circle":
            # Skip synthetic full circle when arc polylines already form the outline
            if has_polys:
                continue
            c = cv.tx(g["cx"], g["cy"])
            r = (g.get("r") or 1) * cv.scale
            d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], outline=col, width=w)
        elif g["kind"] == "poly" and g.get("pts"):
            pts = [cv.tx(p[0], p[1]) for p in g["pts"]]
            if len(pts) >= 2:
                d.line(pts, fill=col, width=w)


def draw_footprint(cv: Canvas, comp: dict) -> None:
    d = cv.draw
    fx, fy, frot = comp["x"], comp["y"], comp["rot"]
    gfx = comp.get("graphics") or []
    if not gfx:
        # fallback box
        w, h = comp.get("w", 1), comp.get("h", 1)
        corners = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
        pts = [cv.board_to_local_img(lx, ly, fx, fy, frot) for lx, ly in corners]
        d.polygon(pts, outline=hex_rgba("F.SilkS"), fill=(40, 40, 50, 180))
        return

    # pads — front view skips pure B.Cu (battery 12 mm ground pad etc.)
    for g in gfx:
        if g.get("kind") != "pad":
            continue
        ly = g.get("layer") or "F.Cu"
        is_back = str(ly).startswith("B")
        if is_back:
            continue  # match default front-side KiCad view
        col = hex_rgba("F.Cu", 200)
        px, py, pr = g.get("x", 0), g.get("y", 0), g.get("rot", 0)
        w, h = g.get("w", 0.5), g.get("h", 0.5)
        large = max(w, h) > 4.0
        shape = (g.get("shape") or "rect").lower()
        if large:
            col = hex_rgba("F.Cu", 50)
        if "circle" in shape or (shape == "oval" and abs(w - h) < 0.05):
            c = cv.board_to_local_img(px, py, fx, fy, frot)
            r = 0.5 * max(w, h) * cv.scale
            if large:
                d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], outline=col, width=2)
            else:
                d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=col)
        else:
            # Board-space pad orientation (not local-then-place — that is 90° off)
            bcorners = pad_corners_board(fx, fy, frot, px, py, pr, w, h)
            pts = [cv.tx(bx, by) for bx, by in bcorners]
            if large:
                d.line(pts + [pts[0]], fill=col, width=2)
            else:
                d.polygon(pts, fill=col)
        if not large and str(g.get("pinfunction") or "").upper() == "K":
            # K strip along +local-X edge of pad, same board-space rot as pad body
            sw = w * 0.2
            strip_local = [
                (w / 2 - sw, -h / 2),
                (w / 2, -h / 2),
                (w / 2, h / 2),
                (w / 2 - sw, h / 2),
            ]
            cx, cy = local_to_board(fx, fy, frot, px, py)
            thr = math.radians(-float(pr or 0))
            cr, sr = math.cos(thr), math.sin(thr)
            pts = []
            for lx, ly_ in strip_local:
                pts.append(cv.tx(cx + lx * cr - ly_ * sr, cy + lx * sr + ly_ * cr))
            d.polygon(pts, fill=(240, 224, 128, 230))

    # silk / fab (front only for comparison with KiCad F.SilkS plot)
    for g in gfx:
        if g.get("kind") == "pad":
            continue
        ly = g.get("layer") or ""
        if str(ly).startswith("B."):
            continue
        if "CrtYd" in ly:
            col = (208, 64, 208, 90)
        elif "Fab" in ly:
            col = (160, 160, 160, 150)
        elif "Silk" in ly:
            col = hex_rgba("F.SilkS")
        elif ".Cu" in ly:
            col = hex_rgba("F.Cu", 160)
        else:
            col = hex_rgba("F.SilkS", 180)
        wpx = max(1, int((g.get("width") or 0.1) * cv.scale))

        if g["kind"] == "line":
            a = cv.board_to_local_img(g["x1"], g["y1"], fx, fy, frot)
            b = cv.board_to_local_img(g["x2"], g["y2"], fx, fy, frot)
            d.line([a, b], fill=col, width=wpx)
        elif g["kind"] == "rect":
            corners = [
                (g["x1"], g["y1"]),
                (g["x2"], g["y1"]),
                (g["x2"], g["y2"]),
                (g["x1"], g["y2"]),
            ]
            pts = [cv.board_to_local_img(lx, ly_, fx, fy, frot) for lx, ly_ in corners]
            d.line(pts + [pts[0]], fill=col, width=wpx)
        elif g["kind"] == "circle":
            pts = []
            for i in range(32):
                a = 2 * math.pi * i / 32
                lx = g["cx"] + g["r"] * math.cos(a)
                ly_ = g["cy"] + g["r"] * math.sin(a)
                pts.append(cv.board_to_local_img(lx, ly_, fx, fy, frot))
            d.line(pts + [pts[0]], fill=col, width=wpx)
        elif g["kind"] == "poly" and g.get("pts"):
            pts = [cv.board_to_local_img(p[0], p[1], fx, fy, frot) for p in g["pts"]]
            if len(pts) >= 2:
                seq = pts + ([pts[0]] if g.get("closed", True) else [])
                d.line(seq, fill=col, width=wpx)


def render_board(board_dict: dict, size: int = 1600) -> Image.Image:
    cv = Canvas(size=size)
    pts = collect_bounds(board_dict)
    cv.fit(pts)
    draw_outline(cv, board_dict.get("outline") or [])
    for c in board_dict.get("components") or []:
        draw_footprint(cv, c)
    # ref labels for key parts
    d = cv.draw
    for c in board_dict.get("components") or []:
        if c["ref"].startswith("D") and c["ref"][1:].isdigit():
            continue  # skip dense LED labels for clarity
        p = cv.tx(c["x"], c["y"])
        d.text((p[0] + 2, p[1] - 10), c["ref"], fill=(232, 208, 96, 230))
    return cv.img.convert("RGB")


def landmark_report(board_dict: dict) -> dict:
    """Key positions after view transform for automated checks."""
    def v(ref: str) -> tuple[float, float, float]:
        c = next(x for x in board_dict["components"] if x["ref"] == ref)
        vx, vy = view_xy(c["x"], c["y"])
        return vx, vy, c["rot"]

    s1 = v("S1")
    h1 = v("H1")
    u1 = v("U1")
    d1 = v("D1")
    d46 = v("D46")
    return {
        "S1_left_of_U1": s1[0] < u1[0],
        "H1_above_U1": h1[1] > u1[1],  # after flip, higher view Y = toward hook top
        "D1_below_U1": d1[1] < u1[1],  # D1 at +Y board → bottom after flip
        "S1_view": s1,
        "H1_view": h1,
        "U1_view": u1,
        "D1_view": d1,
        "D46_view": d46,
        "D45_rot": next(x["rot"] for x in board_dict["components"] if x["ref"] == "D45"),
        "D46_rot": next(x["rot"] for x in board_dict["components"] if x["ref"] == "D46"),
        "rot_step_cw_deg": (
            next(x["rot"] for x in board_dict["components"] if x["ref"] == "D46")
            - next(x["rot"] for x in board_dict["components"] if x["ref"] == "D45")
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pcb", type=Path, default=DEFAULT_PCB)
    ap.add_argument("--config", type=Path, default=DEFAULT_CFG)
    ap.add_argument("-o", "--out", type=Path, default=OUT_DIR / "viewer_2d.png")
    ap.add_argument("--size", type=int, default=1600)
    args = ap.parse_args()

    cfg = load_config(args.config) if args.config.exists() else None
    board = load_board_from_kicad_pcb(args.pcb, cfg)
    bd = board_to_viewer_dict(board, cfg)
    report = landmark_report(bd)
    print("Landmark checks (view space, Y-flip on):")
    for k, v in report.items():
        print(f"  {k}: {v}")
    ok = report["S1_left_of_U1"] and report["H1_above_U1"] and report["rot_step_cw_deg"] > 0
    print("PASS" if ok else "FAIL", "layout landmarks")

    img = render_board(bd, size=args.size)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    img.save(args.out)
    print(f"Wrote {args.out}")

    # also save JSON report
    rep_path = args.out.with_suffix(".landmarks.json")
    import json

    rep_path.write_text(json.dumps(report, indent=2) + "\n")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
