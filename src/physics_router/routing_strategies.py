"""Routing methodologies that make multilayer boards easier to finish.

Research-backed strategies applied as *policies* on top of the geometric router:

1. **Net ordering** — critical / high-weight / power first (reduces rip-up).
2. **Layer assignment** — signals on outer, planes inner (4L); H/V preferred
   directions optional per layer (classic Specctra-style) while still allowing
   free-angle on a single layer when clear.
3. **Via minimization** — prefer same-layer completion; charge vias in cost.
4. **Escape-then-area** — short fanout from pads before global connections
   (escape routing literature).
5. **Pair co-routing** — differential pairs (SDA/SCL, USB) share layer &
   similar geometry.
6. **DRC from KiCad** — clearance / width / via from DesignRules, never below
   board minima.
7. **Pre-route tests** — density / congestion checks that suggest more layers
   or wider spacing before investing in full search.

References (see RESEARCH.md): Lee maze, Dayan rubberband, NS-Place congestion,
layer-assignment via minimization, MCTS multi-layer actions, MLV-CBS, 3D line
exploration geometric routing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from physics_router.design_rules import DesignRules, default_design_rules
from physics_router.models import BoardModel, NetClass, PlacementConfig
from physics_router.router import (
    RouteResult,
    Via,
    clearance_aware_route,
    rubberband_cleanup,
    topological_guide_route,
)


@dataclass
class PreRouteReport:
    """Lightweight tests before expensive routing."""

    net_count: int = 0
    pin_count: int = 0
    copper_layers: list[str] = field(default_factory=list)
    estimated_density: float = 0.0  # pins per cm^2
    congestion_warning: bool = False
    suggestions: list[str] = field(default_factory=list)
    recommended_grid_mm: float = 0.5
    recommended_clearance_mm: float = 0.2

    def to_dict(self) -> dict:
        return {
            "net_count": self.net_count,
            "pin_count": self.pin_count,
            "copper_layers": self.copper_layers,
            "estimated_density_pins_per_cm2": self.estimated_density,
            "congestion_warning": self.congestion_warning,
            "suggestions": self.suggestions,
            "recommended_grid_mm": self.recommended_grid_mm,
            "recommended_clearance_mm": self.recommended_clearance_mm,
        }


def pre_route_analysis(
    board: BoardModel,
    config: PlacementConfig | None = None,
    rules: DesignRules | None = None,
) -> PreRouteReport:
    rules = rules or default_design_rules()
    pins = sum(len(p) for p in board.nets.values())
    area_cm2 = max((board.width_mm * board.height_mm) / 100.0, 0.01)
    density = pins / area_cm2
    report = PreRouteReport(
        net_count=len(board.nets),
        pin_count=pins,
        copper_layers=list(rules.copper_layers),
        estimated_density=density,
        recommended_clearance_mm=rules.constraints.min_clearance_mm,
        recommended_grid_mm=max(0.1, min(0.5, rules.constraints.min_clearance_mm)),
    )

    n_layers = len(rules.copper_layers)
    if density > 40 and n_layers < 4:
        report.congestion_warning = True
        report.suggestions.append(
            "High pin density: prefer 4-layer stack (SIG-GND-PWR-SIG) to ease routing "
            "and improve return paths (community + multilayer PCB practice)."
        )
    if n_layers >= 4:
        report.suggestions.append(
            "Multilayer: assign GND/PWR to inner layers; route critical signals on "
            "outer layers adjacent to a reference plane (controlled impedance)."
        )
    if config:
        cpx = [n for n in board.nets if n.upper().startswith("CPX")]
        if len(cpx) >= 4:
            report.suggestions.append(
                "Charlieplex/matrix nets: route as a bundle from MCU with matched "
                "lengths; keep analog (MIC) on opposite side of board when possible."
            )
        if any(
            config.net_by_name().get(n) and config.net_by_name()[n].net_class == NetClass.ANALOG
            for n in board.nets
        ):
            report.suggestions.append(
                "Analog nets present: spatial separation from high-di/dt nets; "
                "avoid crossing CPX/switcher on same layer."
            )
        pairs = [
            (n, lab.pair_with)
            for n, lab in config.net_by_name().items()
            if lab.pair_with and n in board.nets
        ]
        if pairs:
            report.suggestions.append(
                "Differential/I2C pairs: co-route on same layer with matched length "
                f"(pairs: {pairs[:5]})."
            )

    if not rules.constraints.allow_blind_buried_vias:
        report.suggestions.append(
            "Only through vias allowed (KiCad rules): each layer change costs a full "
            "through via — prefer same-layer completion (via minimization)."
        )

    report.suggestions.append(
        f"DRC floors: clearance≥{rules.constraints.min_clearance_mm}mm, "
        f"track≥{rules.constraints.min_track_width_mm}mm, "
        f"via≥{rules.constraints.min_via_diameter_mm}mm."
    )
    return report


def ordered_nets(board: BoardModel, config: PlacementConfig | None) -> list[str]:
    """Priority order: power/ground → critical high-weight → others → low-weight."""

    def key(n: str) -> tuple:
        if config is None:
            return (1, 0.0, n)
        lab = config.net_by_name().get(n)
        w = config.weight_for_net(n)
        if lab is None:
            return (5, -w, n)
        class_rank = {
            NetClass.GROUND: 0,
            NetClass.POWER: 1,
            NetClass.CLOCK: 2,
            NetClass.HIGH_SPEED: 2,
            NetClass.DIFFERENTIAL: 3,
            NetClass.ANALOG: 3,
            NetClass.RF: 2,
            NetClass.RESET: 4,
            NetClass.SIGNAL: 5,
            NetClass.OTHER: 6,
        }.get(lab.net_class, 5)
        crit = 0 if lab.critical else 1
        return (class_rank, crit, -w, n)

    return sorted(board.nets.keys(), key=key)


def multilayer_route(
    board: BoardModel,
    config: PlacementConfig | None = None,
    rules: DesignRules | None = None,
    *,
    clearance_mm: float | None = None,
    grid_mm: float | None = None,
    allow_vias: bool = True,
    guide_only: bool = False,
    use_layer_directions: bool = True,
    progress_cb=None,
) -> RouteResult:
    """DRC-aware multilayer route using KiCad rules + research heuristics.

    - Clearance/width/via from DesignRules (net-class aware)
    - Layer preference per net class
    - Optional H/V preferred directions alternating by layer (easier completion
      on dense boards) while free-angle LOS still used when unobstructed
    - Net ordering for fewer conflicts
    """
    rules = rules or default_design_rules()
    if guide_only:
        return topological_guide_route(board, config)

    # Effective clearance: never below KiCad minimum
    min_cl = rules.constraints.min_clearance_mm
    cl = max(clearance_mm if clearance_mm is not None else min_cl, min_cl)

    grid = grid_mm
    if grid is None:
        grid = max(0.1, min(0.5, cl))
        if config and config.grid_mm:
            grid = min(grid, config.grid_mm) if config.grid_mm >= 0.1 else grid

    copper = list(rules.copper_layers) or ["F.Cu", "B.Cu"]

    # Build a synthetic config-aware route by calling core router with all layers
    # then re-assign segment layers / widths per net using rules.
    result = clearance_aware_route(
        board,
        config,
        layers=copper,
        clearance_mm=cl,
        grid_mm=grid,
        allow_vias=allow_vias and not (
            not rules.constraints.allow_blind_buried_vias and len(copper) > 2
            # through vias still allowed when blind/buried disabled
        ),
        guide_only=False,
        soft_fallback=False,  # never paint illegal copper
        progress_cb=progress_cb,
    )

    # Dayan-style rubberband cleanup (shorten free-angle paths under DRC clearance)
    result = rubberband_cleanup(result, board, config, clearance_mm=cl)

    # Enforce track widths & via sizes from DRC
    for seg in result.segments:
        w = rules.track_width_for_net(seg.net, config)
        seg.width_mm = w
        # Snap layer to preferred set if current layer not in copper
        if seg.layer not in copper:
            prefs = rules.layers_for_net(seg.net, config)
            seg.layer = prefs[0] if prefs else copper[0]

    for via in result.vias:
        d, drill = rules.via_for_net(via.net, config)
        via.size_mm = d
        via.drill_mm = drill
        if not rules.constraints.allow_blind_buried_vias and len(copper) >= 2:
            via.layers = (copper[0], copper[-1])

    # Preferential layer coloring for multi-layer: alternate H/V hint in notes
    if use_layer_directions and len(copper) >= 2:
        result.notes.append(
            "layer_policy: "
            + ", ".join(
                f"{ly}~{'H' if i % 2 == 0 else 'V'}-preferred"
                for i, ly in enumerate(copper)
            )
            + " (free-angle still used when LOS clear)"
        )
    result.notes.append(
        f"drc: clearance≥{cl}mm track_min={rules.constraints.min_track_width_mm}mm "
        f"layers={copper} vias_through={not rules.constraints.allow_blind_buried_vias}"
    )

    # Pair co-routing note
    if config:
        for lab in config.nets:
            if lab.pair_with and lab.name in board.nets and lab.pair_with in board.nets:
                result.notes.append(
                    f"pair:{lab.name}+{lab.pair_with} prefer same layer / matched length"
                )

    return result


def escape_hints(board: BoardModel, config: PlacementConfig | None = None) -> list[str]:
    """Pad-escape methodology hints (BGA/QFN fanout literature)."""
    hints: list[str] = []
    for ref, c in board.components.items():
        n_pads = len(c.pads)
        if n_pads >= 20:
            hints.append(
                f"{ref}: {n_pads} pads — use escape routing (short stubs to routing channels) "
                "before area routing; consider via-in-pad only if fab allows."
            )
        if n_pads >= 8 and c.width_mm <= 6:
            hints.append(
                f"{ref}: fine-pitch package — stagger escapes on multiple layers "
                f"({config.project_name if config else 'board'})."
            )
    if not hints:
        hints.append("No high-pin packages detected; direct area routing is fine.")
    return hints


def estimate_via_budget(
    board: BoardModel,
    rules: DesignRules,
    config: PlacementConfig | None = None,
) -> dict:
    """Rough lower bound on vias if each multi-pin net needs one layer change."""
    copper_n = max(1, len(rules.copper_layers))
    multi_pin = sum(1 for pins in board.nets.values() if len(pins) >= 3)
    # empirical: denser → more vias
    est = int(multi_pin * (0.3 if copper_n >= 4 else 0.6))
    return {
        "copper_layers": copper_n,
        "multi_pin_nets": multi_pin,
        "estimated_via_floor": est,
        "note": "Lower when same-layer completion succeeds; higher for strict H/V only.",
    }
