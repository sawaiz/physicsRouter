"""Specctra DSN export for FreeRouting and other Specctra-compatible autorouters.

FreeRouting-friendly DSN: structure, placement with real pad XY, library pin
images, network connectivity, per-net class rules. Units: mil (0.001 inch).
"""

from __future__ import annotations

from pathlib import Path

from physics_router.design_rules import DesignRules, default_design_rules
from physics_router.kicad_io import local_to_board
from physics_router.models import BoardModel, PlacementConfig


def _mm_to_mil(mm: float) -> int:
    return int(round(mm / 0.0254))


def _pad_local_mil(pad: dict) -> tuple[int, int]:
    """Footprint-local pad offset in mil (for DSN library images)."""
    return _mm_to_mil(float(pad.get("x") or 0.0)), _mm_to_mil(float(pad.get("y") or 0.0))


def export_dsn(
    board: BoardModel,
    out_path: str | Path,
    config: PlacementConfig | None = None,
    rules: DesignRules | None = None,
    board_name: str = "physics_router_board",
) -> Path:
    """Write a Specctra DSN file from BoardModel (+ optional labels/rules)."""
    rules = rules or default_design_rules()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    xs = [c.x_mm for c in board.components.values()] or [0.0, board.width_mm]
    ys = [c.y_mm for c in board.components.values()] or [0.0, board.height_mm]
    # Expand bounds with pad extents so free-angle pads near edges stay in-box
    for c in board.components.values():
        for pad in c.pads or []:
            bx, by = local_to_board(
                c.x_mm,
                c.y_mm,
                c.rotation_deg,
                float(pad.get("x") or 0.0),
                float(pad.get("y") or 0.0),
            )
            xs.append(bx)
            ys.append(by)
    min_x, max_x = min(xs) - 2.0, max(xs) + 2.0
    min_y, max_y = min(ys) - 2.0, max(ys) + 2.0
    min_x = min(min_x, 0.0)
    min_y = min(min_y, 0.0)
    max_x = max(max_x, board.width_mm)
    max_y = max(max_y, board.height_mm)

    def mil(mm: float, axis: str = "x") -> int:
        if axis == "x":
            return _mm_to_mil(mm - min_x)
        return _mm_to_mil(mm - min_y)

    w_mil = max(100, _mm_to_mil(max_x - min_x))
    h_mil = max(100, _mm_to_mil(max_y - min_y))

    copper = list(rules.copper_layers) or list(board.copper_layers) or ["F.Cu", "B.Cu"]
    layer_map = {ly: f"Signal_{i}" for i, ly in enumerate(copper)}

    default_w = rules.constraints.min_track_width_mm
    default_cl = rules.constraints.min_clearance_mm
    via_d, via_drill = (
        (rules.via_presets[0]["diameter"], rules.via_presets[0]["drill"])
        if rules.via_presets
        else (rules.constraints.min_via_diameter_mm, rules.constraints.min_via_drill_mm)
    )

    lines: list[str] = []
    lines.append(f"(pcb {board_name}")
    lines.append("  (parser")
    lines.append('    (string_quote ")')
    lines.append("    (space_in_quoted_tokens on)")
    lines.append('    (host_cad "physics_router")')
    lines.append('    (host_version "0.2")')
    lines.append("  )")
    lines.append("  (resolution mil 10)")
    lines.append("  (unit mil)")
    lines.append("  (structure")
    for i, ly in enumerate(copper):
        direction = "horizontal" if i % 2 == 0 else "vertical"
        lines.append(
            f'    (layer "{layer_map[ly]}" (type signal) '
            f"(property (index {i}) (preferred_direction {direction})))"
        )
    lines.append("    (boundary")
    lines.append("      (path pcb 0")
    lines.append(f"        0 0  {w_mil} 0  {w_mil} {h_mil}  0 {h_mil}  0 0")
    lines.append("      )")
    lines.append("    )")
    lines.append(
        f'    (via "Via[0-1]_0:{_mm_to_mil(via_d)}:{_mm_to_mil(via_drill)}")'
    )
    lines.append(
        f"    (rule (width {_mm_to_mil(default_w)}) (clearance {_mm_to_mil(default_cl)}))"
    )
    # Keepout zones as keepout paths when present
    for zone in getattr(board, "zones", None) or []:
        if not zone.get("keepout"):
            continue
        pts = zone.get("points") or []
        if len(pts) < 3:
            continue
        path = " ".join(f"{mil(float(p[0]), 'x')} {mil(float(p[1]), 'y')}" for p in pts)
        lines.append(f"    (keepout (polygon signal 0 {path}))")
    lines.append("  )")

    # library images with real pad offsets
    lines.append("  (library")
    for ref, c in board.components.items():
        img = f"IMG_{ref}"
        pw = max(4, _mm_to_mil(c.width_mm / 2))
        ph = max(4, _mm_to_mil(c.height_mm / 2))
        lines.append(f'    (image "{img}"')
        lines.append(f"      (outline (rect signal -{pw} -{ph} {pw} {ph}))")
        if c.pads:
            for i, pad in enumerate(c.pads):
                pnum = str(pad.get("num", i + 1)).replace('"', "")
                ox, oy = _pad_local_mil(pad)
                # Approximate pad diameter for padstack selection
                sz = max(
                    abs(float(pad.get("w") or 0.5)),
                    abs(float(pad.get("h") or 0.5)),
                    0.3,
                )
                pstack = "Pad" if sz < 1.2 else "PadLarge"
                lines.append(f'      (pin "{pstack}" "{pnum}" {ox} {oy})')
        else:
            lines.append('      (pin "Pad" "1" 0 0)')
        lines.append("    )")

    for pstack, diam_mil in (("Pad", 20), ("PadLarge", 40)):
        lines.append(f'    (padstack "{pstack}"')
        for ly in copper:
            lines.append(f'      (shape (circle "{layer_map[ly]}" {diam_mil} 0 0))')
        lines.append("    )")

    lines.append(
        f'    (padstack "Via[0-1]_0:{_mm_to_mil(via_d)}:{_mm_to_mil(via_drill)}"'
    )
    for ly in copper:
        lines.append(
            f'      (shape (circle "{layer_map[ly]}" {_mm_to_mil(via_d)} 0 0))'
        )
    lines.append("      (attach off)")
    lines.append("    )")
    lines.append("  )")

    # placement
    lines.append("  (placement")
    for ref, c in board.components.items():
        side = "front" if (c.layer or "F.Cu").startswith("F") else "back"
        rot = int(round(c.rotation_deg)) % 360
        lines.append(f'    (component "IMG_{ref}"')
        lines.append(
            f'      (place "{ref}" {mil(c.x_mm, "x")} {mil(c.y_mm, "y")} {side} {rot})'
        )
        lines.append("    )")
    lines.append("  )")

    # network with class rules
    lines.append("  (network")
    # Emit class definitions first when config present
    if config and config.nets:
        seen_cls: set[str] = set()
        for lab in config.nets:
            cname = lab.net_class.value if lab.net_class else "signal"
            if cname in seen_cls:
                continue
            seen_cls.add(cname)
            w = _mm_to_mil(rules.track_width_for_net(lab.name, config))
            cl = _mm_to_mil(rules.clearance_for_net(lab.name, config))
            lines.append(f'    (class "{cname}"')
            lines.append(f"      (rule (width {w}) (clearance {cl}))")
            lines.append("    )")

    for net_name, pins in board.nets.items():
        if not net_name:
            continue
        safe = net_name.replace('"', "'")
        pin_toks = []
        for ref, pad in pins:
            if ref not in board.components:
                continue
            p = str(pad).replace('"', "")
            pin_toks.append(f'"{ref}"-"{p}"')
        if len(pin_toks) < 2:
            continue
        lines.append(f'    (net "{safe}"')
        lines.append("      (pins " + " ".join(pin_toks) + ")")
        if config:
            w = _mm_to_mil(rules.track_width_for_net(net_name, config))
            cl = _mm_to_mil(rules.clearance_for_net(net_name, config))
            lines.append(f"      (rule (width {w}) (clearance {cl}))")
            lab = config.net_by_name().get(net_name)
            if lab and lab.pair_with:
                lines.append(f'      (fromto "{safe}" "{lab.pair_with.replace(chr(34), chr(39))}")')
            if lab and lab.net_class:
                lines.append(f'      (use_net "{lab.net_class.value}")')
        lines.append("    )")
    lines.append("  )")

    lines.append("  (wiring")
    lines.append("  )")
    lines.append(")")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def write_freerouting_readme(out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "FREEROUTING.md"
    p.write_text(
        """# FreeRouting baseline

1. Export DSN from physics-router:
   ```bash
   physics-router export-dsn --config examples/halo-90/placement_config.yaml \\
     --pcb third_party/halo-90/pcb/halo-90.kicad_pcb -o compare/board.dsn
   ```

2. Run FreeRouting (GUI or CLI jar):
   ```bash
   java -jar freerouting.jar -de board.dsn -do board.ses -mp 100
   ```
   Download: https://github.com/freerouting/freerouting

3. Import SES wires back (optional):
   ```bash
   physics-router import-ses --ses board.ses --pcb board.kicad_pcb -o board_fr.kicad_pcb
   ```

4. Compare metrics:
   ```bash
   physics-router compare-routes \\
     --topor compare/topor_route.json \\
     --dsn compare/board.dsn \\
     --ses compare/board.ses \\
     --out compare/comparison.json
   ```

If FreeRouting is not installed, compare still works with TopoR-only results and
documents the baseline procedure for CI/manual runs.
""",
        encoding="utf-8",
    )
    return p
