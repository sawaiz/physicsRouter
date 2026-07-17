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
    build_obstacle_map,
    clearance_aware_route,
    remove_redundant_vias,
    rubberband_cleanup,
    topological_guide_route,
)
from physics_router.topology import (
    CongestionMap,
    pareto_front,
    score_vector_from_route,
    signatures_from_route,
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


def _variant_score(result: RouteResult) -> float:
    """Multiobjective rank: completion first, then quality score, then length."""
    q = result.quality or result.compute_quality()
    completion = float(q.get("completion") or 0.0)
    score = float(q.get("score") or 0.0)
    # Prefer more routed copper; then higher grade; break ties with shorter length
    return (
        completion * 1e6
        + score * 1e3
        - float(result.total_length_mm)
        - float(result.via_count) * 5.0
        - float(result.clearance_violations) * 20.0
    )


def _net_order_variants(board: BoardModel, config: PlacementConfig | None) -> list[tuple[str, list[str]]]:
    """Alternative paint orders for multi-variant TopoR-style search."""
    base = ordered_nets(board, config)
    # Small-first (less blockage early) — default clearance_aware uses pin-count within priority
    small_first = sorted(
        board.nets.keys(),
        key=lambda n: (len(board.nets[n]), -((config.weight_for_net(n) if config else 1.0)), n),
    )
    large_first = list(reversed(small_first))
    # Critical nets last so they see remaining free channels
    if config:
        crit_last = sorted(
            board.nets.keys(),
            key=lambda n: (
                0 if not (config.net_by_name().get(n) and config.net_by_name()[n].critical) else 1,
                -config.weight_for_net(n),
                n,
            ),
        )
    else:
        crit_last = list(reversed(base))
    return [
        ("priority", base),
        ("small_first", small_first),
        ("large_first", large_first),
        ("critical_last", crit_last),
    ]


def _apply_drc_geometry(
    result: RouteResult,
    board: BoardModel,
    config: PlacementConfig | None,
    rules: DesignRules,
    cl: float,
) -> RouteResult:
    """Geometry polish: rubberband → via minimize → DRC widths."""
    copper = list(rules.copper_layers) or ["F.Cu", "B.Cu"]
    # Phase C: re-geometrize (Dayan/TopoR rubberband)
    result = rubberband_cleanup(result, board, config, clearance_mm=cl)
    # Phase C: remove unnecessary vias when same-layer stubs are legal
    result = remove_redundant_vias(result, board, config, clearance_mm=cl)

    for seg in result.segments:
        w = rules.track_width_for_net(seg.net, config)
        seg.width_mm = w
        if seg.layer not in copper:
            prefs = rules.layers_for_net(seg.net, config)
            seg.layer = prefs[0] if prefs else copper[0]

    for via in result.vias:
        d, drill = rules.via_for_net(via.net, config)
        via.size_mm = d
        via.drill_mm = drill
        if not rules.constraints.allow_blind_buried_vias and len(copper) >= 2:
            via.layers = (copper[0], copper[-1])

    result.notes.append(
        f"drc: clearance≥{cl}mm track_min={rules.constraints.min_track_width_mm}mm "
        f"layers={copper} vias_through={not rules.constraints.allow_blind_buried_vias}"
    )
    if config:
        for lab in config.nets:
            if lab.pair_with and lab.name in board.nets and lab.pair_with in board.nets:
                result.notes.append(
                    f"pair:{lab.name}+{lab.pair_with} prefer same layer / matched length"
                )
    result.compute_quality()
    return result


def topor_style_route(
    board: BoardModel,
    config: PlacementConfig | None = None,
    rules: DesignRules | None = None,
    *,
    clearance_mm: float | None = None,
    grid_mm: float | None = None,
    allow_vias: bool = True,
    guide_only: bool = False,
    num_variants: int | None = None,
    negotiate_iters: int | None = None,
    progress_cb=None,
) -> RouteResult:
    """TopoR-inspired pipeline (docs/TOPOR.md + docs/ARCHITECTURE_ROUTER.md).

    Phases
    ------
    A. Instant topology — free-angle connectivity under clearance (no illegal soft copper).
    B. Multi-variant + negotiated congestion — alternate net orders; raise historical
       cost of crowded cells so later iterations spread into other homotopy classes.
    C. Geometry polish — rubberband shorten + redundant via removal.
    D. DRC floors — track/via sizes from KiCad design rules.
    E. Topology signatures — record obstacle-side classes for explainability.

    Style is **isotropic** (no preferred H/V layer directions). Soft fallback stays
    off so open edges beat overlapping copper.
    """
    rules = rules or default_design_rules()
    if guide_only:
        r = topological_guide_route(board, config)
        r.notes.append("topor_pipeline: guide_only (topology sketch, no clearance)")
        return r

    min_cl = rules.constraints.min_clearance_mm
    cl = max(clearance_mm if clearance_mm is not None else min_cl, min_cl)
    grid = grid_mm
    if grid is None:
        grid = max(0.1, min(0.5, cl))
        if config and config.grid_mm and config.grid_mm >= 0.1:
            grid = min(grid, config.grid_mm)

    copper = list(rules.copper_layers) or list(board.copper_layers) or ["F.Cu", "B.Cu"]
    n_nets = len(board.nets)
    # Auto variant budget: dense boards stay single-pass for interactive UI latency
    if num_variants is None:
        if n_nets > 40:
            num_variants = 1
        elif n_nets > 16:
            num_variants = 2
        else:
            num_variants = 3
    num_variants = max(1, min(4, int(num_variants)))

    # Negotiated congestion iterations (route → measure → raise historical cost).
    # Default 1 for interactive latency; CLI/batch can pass negotiate_iters=2–3.
    if negotiate_iters is None:
        negotiate_iters = 1
    negotiate_iters = max(1, min(3, int(negotiate_iters)))

    orders = _net_order_variants(board, config)[:num_variants]
    variant_specs: list[dict] = []
    for name, order in orders:
        variant_specs.append(
            {
                "name": name,
                "net_order": order,
                "allow_vias": allow_vias,
                "grid_mm": grid,
            }
        )
    if num_variants >= 2 and allow_vias:
        variant_specs[-1] = {
            "name": "via_averse",
            "net_order": orders[0][1],
            "allow_vias": True,
            "grid_mm": max(0.15, grid * 0.85),
        }

    cong = CongestionMap(cell_mm=max(0.5, grid))
    candidates: list[tuple[str, RouteResult]] = []
    scored: list[tuple[str, RouteResult, object]] = []

    for ni in range(negotiate_iters):
        for vi, spec in enumerate(variant_specs):
            label = spec["name"] if negotiate_iters == 1 else f"{spec['name']}_n{ni}"
            cb = progress_cb if (vi == 0 and ni == 0) else None
            if progress_cb and not (vi == 0 and ni == 0):
                try:
                    progress_cb(
                        0,
                        1,
                        label,
                        "variant",
                        {"variant": label, "index": vi, "negotiate": ni},
                    )
                except Exception:
                    pass
            raw = clearance_aware_route(
                board,
                config,
                layers=copper,
                clearance_mm=cl,
                grid_mm=float(spec["grid_mm"]),
                allow_vias=bool(spec["allow_vias"]),
                guide_only=False,
                soft_fallback=False,
                prefer_native=False,
                progress_cb=cb,
                net_order=list(spec["net_order"]),
                style="isotropic",
                congestion=cong,
            )
            polished = _apply_drc_geometry(raw, board, config, rules, cl)
            polished.notes.append(f"topor_variant: {label}")
            # Present congestion from this solution (for negotiation + scoring)
            cong.clear_present()
            cong.paint_route(polished)
            sv = score_vector_from_route(polished, cong)
            polished.quality = {
                **(polished.quality or {}),
                "variant": label,
                "pipeline": "topor_style",
                "score_vector": sv.to_dict(),
                "negotiate_iter": ni,
            }
            candidates.append((label, polished))
            scored.append((label, polished, sv))
        # Feed geometric crowding back into historical costs (next iteration)
        if ni + 1 < negotiate_iters:
            cong.negotiate()
            if progress_cb:
                try:
                    progress_cb(0, 1, "negotiate", "congestion", {"iter": ni})
                except Exception:
                    pass

    # Pareto front of complete-board variants, then pick best scalar among front
    front = pareto_front(scored)  # type: ignore[arg-type]
    best_name, best, best_sv = max(front, key=lambda t: _variant_score(t[1]))
    best.notes.insert(
        0,
        f"topor_pipeline: isotropic free-angle · {len(candidates)} candidate(s) · "
        f"negotiate_iters={negotiate_iters} · winner={best_name} · "
        f"pareto={len(front)}",
    )

    # Topology signatures for explainability
    try:
        om = build_obstacle_map(board, clearance_mm=cl, layers=copper)
        for s in best.segments:
            om.paint_trace(s.x1, s.y1, s.x2, s.y2, s.layer, s.width_mm, s.net)
        sigs = signatures_from_route(best, om)
        best.quality = {**(best.quality or {}), "topology_signatures": sigs[:40]}
        best.notes.append(f"topology_signatures: {len(sigs)} net class(es) recorded")
    except Exception as exc:
        best.notes.append(f"topology_signatures: skipped ({exc})")

    ranking = sorted(
        (
            {
                "name": n,
                "score": (r.quality or {}).get("score"),
                "grade": (r.quality or {}).get("grade"),
                "length_mm": round(r.total_length_mm, 2),
                "vias": r.via_count,
                "unrouted": len(r.unrouted_nets),
                "violations": r.clearance_violations,
                "score_vector": (r.quality or {}).get("score_vector"),
                "pareto": any(n == f[0] for f in front),
            }
            for n, r in candidates
        ),
        key=lambda d: -(d.get("score") or 0),
    )
    best.notes.append(
        "variants: "
        + ", ".join(
            f"{d['name']}={d.get('grade')}/{d.get('score')} "
            f"L={d['length_mm']} V={d['vias']} U={d['unrouted']}"
            for d in ranking[:6]
        )
    )
    best.compute_quality()
    best.quality["variants_ranked"] = ranking
    best.quality["winner"] = best_name
    best.quality["pipeline"] = "topor_style"
    best.quality["pareto_front"] = [f[0] for f in front]
    best.quality["score_vector"] = best_sv.to_dict() if hasattr(best_sv, "to_dict") else {}
    return best


def multilayer_route(
    board: BoardModel,
    config: PlacementConfig | None = None,
    rules: DesignRules | None = None,
    *,
    clearance_mm: float | None = None,
    grid_mm: float | None = None,
    allow_vias: bool = True,
    guide_only: bool = False,
    use_layer_directions: bool = False,
    num_variants: int | None = None,
    progress_cb=None,
) -> RouteResult:
    """DRC-aware multilayer TopoR-style route (isotropic free-angle).

    Delegates to :func:`topor_style_route`. ``use_layer_directions`` is retained
    for API compatibility but defaults **False** — TopoR-style isotropic routing
    does not assign preferred H/V directions per layer.
    """
    result = topor_style_route(
        board,
        config,
        rules,
        clearance_mm=clearance_mm,
        grid_mm=grid_mm,
        allow_vias=allow_vias,
        guide_only=guide_only,
        num_variants=num_variants,
        progress_cb=progress_cb,
    )
    if use_layer_directions:
        copper = list((rules or default_design_rules()).copper_layers) or ["F.Cu", "B.Cu"]
        if len(copper) >= 2:
            result.notes.append(
                "layer_policy(optional): "
                + ", ".join(
                    f"{ly}~{'H' if i % 2 == 0 else 'V'}-hint"
                    for i, ly in enumerate(copper)
                )
                + " — isotropic paths still preferred when LOS clear"
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
