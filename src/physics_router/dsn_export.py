"""Specctra DSN export for FreeRouting and other Specctra-compatible autorouters.

Minimal but FreeRouting-friendly DSN: structure, placement, library pin images,
network connectivity. Units: mil (0.001 inch) for broad compatibility.
"""

from __future__ import annotations

from pathlib import Path

from physics_router.design_rules import DesignRules, default_design_rules
from physics_router.models import BoardModel, PlacementConfig


def _mm_to_mil(mm: float) -> int:
    return int(round(mm / 0.0254))


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

    # Board bounds in mil (origin bottom-left for DSN simplicity)
    # Shift so all coords positive
    xs = [c.x_mm for c in board.components.values()] or [0.0, board.width_mm]
    ys = [c.y_mm for c in board.components.values()] or [0.0, board.height_mm]
    min_x, max_x = min(xs) - 2.0, max(xs) + 2.0
    min_y, max_y = min(ys) - 2.0, max(ys) + 2.0
    # Use board size if larger
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
    # Specctra layer names: use simplified tokens
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
    lines.append("    (host_cad \"physics_router\")")
    lines.append("    (host_version \"0.1\")")
    lines.append("  )")
    lines.append("  (resolution mil 10)")
    lines.append("  (unit mil)")
    lines.append("  (structure")
    for ly in copper:
        # preferred direction alternating H/V
        i = copper.index(ly)
        direction = "horizontal" if i % 2 == 0 else "vertical"
        lines.append(f'    (layer "{layer_map[ly]}" (type signal) (property (index {i}) (preferred_direction {direction})))')
    # boundary rectangle
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
    # per-class rules
    if config:
        for lab in config.nets:
            w = rules.track_width_for_net(lab.name, config)
            cl = rules.clearance_for_net(lab.name, config)
            # class rule attached later via net
            _ = (w, cl)
    lines.append("  )")

    # library images (one per unique footprint or per component)
    lines.append("  (library")
    for ref, c in board.components.items():
        img = f"IMG_{ref}"
        pw = max(4, _mm_to_mil(c.width_mm / 2))
        ph = max(4, _mm_to_mil(c.height_mm / 2))
        lines.append(f'    (image "{img}"')
        # outline
        lines.append(
            f"      (outline (rect signal -{pw} -{ph} {pw} {ph}))"
        )
        # pins from pads or single pin at origin
        if c.pads:
            for i, pad in enumerate(c.pads):
                pnum = str(pad.get("num", i + 1)).replace('"', "")
                # place pads in a small grid if no offset
                ox = (i % 4) * 10 - 15
                oy = (i // 4) * 10 - 15
                lines.append(f'      (pin "Pad" "{pnum}" {ox} {oy})')
        else:
            lines.append('      (pin "Pad" "1" 0 0)')
        lines.append("    )")
    # padstack
    lines.append('    (padstack "Pad"')
    for ly in copper:
        lines.append(f'      (shape (circle "{layer_map[ly]}" 20 0 0))')
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
        side = "front"
        rot = int(round(c.rotation_deg)) % 360
        lines.append(f'    (component "IMG_{ref}"')
        lines.append(
            f'      (place "{ref}" {mil(c.x_mm, "x")} {mil(c.y_mm, "y")} {side} {rot})'
        )
        lines.append("    )")
    lines.append("  )")

    # network
    lines.append("  (network")
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
        # rule from labels
        if config:
            w = _mm_to_mil(rules.track_width_for_net(net_name, config))
            cl = _mm_to_mil(rules.clearance_for_net(net_name, config))
            lines.append(f"      (rule (width {w}) (clearance {cl}))")
        lines.append("    )")
    lines.append("  )")

    # empty wiring — FreeRouting fills this / SES
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

3. Compare metrics:
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
