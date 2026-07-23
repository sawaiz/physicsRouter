"""Post-route SPICE / OpenEMS feedback into place · topology · route · pours.

Pipeline (only after a **fully legal** route):

  place → topology → route (+ pours) → DRC gate
       ↘ if complete & 0 hard DRC:
            SPICE proxy (+ ngspice if present)
            OpenEMS proxy (+ export hook if binary present)
         → feedback deltas for next improve round

Physics scores never override open>short: incomplete or illegal copper skips EM.
"""

from __future__ import annotations

import copy
import math
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from physics_router.models import BoardModel, NetClass, PlacementConfig
from physics_router.physics import (
    GeometricSpiceProxy,
    OpenEMSBackend,
    apply_simulation_scores,
    emi_proxy,
    geometric_score,
    matrix_length_match_score,
    power_loop_area,
    return_path_score,
)
from physics_router.router import CopperArea, RouteResult, _dist


@dataclass
class PhysicsFeedback:
    """Structured EM/SPICE scores + actionable deltas for the improve loop."""

    eligible: bool
    reason: str = ""
    spice_cost: float = 0.0
    openems_cost: float = 0.0
    combined_cost: float = 0.0
    geometric_total: float = 0.0
    notes: list[str] = field(default_factory=list)
    # Deltas applied (or proposed) for next round
    placement_weight_bumps: dict[str, float] = field(default_factory=dict)
    topology_hints: list[str] = field(default_factory=list)
    routing_hints: list[str] = field(default_factory=list)
    pour_actions: list[dict[str, Any]] = field(default_factory=list)
    # Generated pours for this round
    copper_areas: list[CopperArea] = field(default_factory=list)
    export_paths: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "spice_cost": round(self.spice_cost, 4),
            "openems_cost": round(self.openems_cost, 4),
            "combined_cost": round(self.combined_cost, 4),
            "geometric_total": round(self.geometric_total, 4),
            "notes": list(self.notes),
            "placement_weight_bumps": dict(self.placement_weight_bumps),
            "topology_hints": list(self.topology_hints),
            "routing_hints": list(self.routing_hints),
            "pour_actions": list(self.pour_actions),
            "copper_areas": len(self.copper_areas),
            "export_paths": dict(self.export_paths),
            "raw": self.raw,
        }


def route_is_physics_eligible(
    route: RouteResult,
    board: BoardModel,
    *,
    require_complete: bool = True,
) -> tuple[bool, str]:
    """True only when copper is legal enough for EM/SPICE feedback."""
    if int(route.clearance_violations or 0) > 0:
        return False, "hard_drc>0"
    q = route.quality or {}
    gate = q.get("manufacturing_gate") if isinstance(q, dict) else None
    if isinstance(gate, dict):
        if int(gate.get("native_drc_violations") or 0) > 0:
            return False, "manufacturing_gate_drc"
    unrouted = list(route.unrouted_nets or [])
    if require_complete and unrouted:
        return False, f"unrouted:{len(unrouted)}"
    if require_complete and board.nets:
        # Also require no missing multipin copper for board nets
        from physics_router.router import _net_fully_connected

        incomplete = [
            n
            for n in board.nets
            if not _net_fully_connected(
                board, n, route.segments, route.vias, areas=route.areas or []
            )
        ]
        if incomplete:
            return False, f"incomplete_nets:{len(incomplete)}"
    if not route.segments and not route.areas:
        return False, "no_copper"
    return True, "ok"


def _route_length_by_net(route: RouteResult) -> dict[str, float]:
    out: dict[str, float] = {}
    for s in route.segments:
        if not s.net:
            continue
        out[s.net] = out.get(s.net, 0.0) + math.hypot(s.x2 - s.x1, s.y2 - s.y1)
    return out


def _route_via_count_by_net(route: RouteResult) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in route.vias:
        if not v.net:
            continue
        out[v.net] = out.get(v.net, 0) + 1
    return out


def route_aware_spice_cost(
    board: BoardModel,
    config: PlacementConfig,
    route: RouteResult,
) -> tuple[float, str]:
    """SPICE-oriented cost using actual routed length/vias on power nets."""
    base = GeometricSpiceProxy()
    cost, note = base.score(board, config)
    lengths = _route_length_by_net(route)
    vias = _route_via_count_by_net(route)
    power_len = 0.0
    power_vias = 0
    for lab in config.nets:
        if lab.net_class not in (NetClass.POWER, NetClass.GROUND) and not lab.simulate_spice:
            continue
        power_len += lengths.get(lab.name, 0.0)
        power_vias += vias.get(lab.name, 0)
    # Via inductance tax ~ 0.5 nH each (proxy); length ~ 0.08 nH/mm microstrip-ish
    via_tax = power_vias * 0.5
    len_tax = power_len * 0.08
    cost = 0.5 * cost + 0.3 * len_tax + 0.2 * via_tax
    note = f"{note}; route_L={power_len:.1f}mm route_vias={power_vias}"
    if shutil.which("ngspice"):
        note += "; ngspice_available"
    return cost, note


def route_aware_openems_cost(
    board: BoardModel,
    config: PlacementConfig,
    route: RouteResult,
) -> tuple[float, str]:
    """OpenEMS-oriented cost: EMI proxy + routed critical length + via hops."""
    base = OpenEMSBackend()
    cost, note = base.score(board, config)
    lengths = _route_length_by_net(route)
    vias = _route_via_count_by_net(route)
    crit_len = 0.0
    crit_vias = 0
    for lab in config.nets:
        if not (
            lab.emi_sensitive
            or lab.simulate_em
            or lab.net_class in (NetClass.RF, NetClass.CLOCK, NetClass.HIGH_SPEED)
        ):
            continue
        crit_len += lengths.get(lab.name, 0.0)
        crit_vias += vias.get(lab.name, 0)
    # Layer hops hurt return path continuity (physics)
    cost = 0.55 * cost + 0.3 * (crit_len * 0.05) + 0.15 * (crit_vias * 1.2)
    note = f"{note}; crit_route_L={crit_len:.1f}mm crit_vias={crit_vias}"
    return cost, note


def propose_power_pours(
    board: BoardModel,
    config: PlacementConfig,
    route: RouteResult,
    *,
    margin_mm: float = 1.5,
) -> list[CopperArea]:
    """Grow simple GND/power pours around pad clouds when return-path is weak.

    KiCad remains the fill oracle; these areas seed native obstacles / export.
    """
    areas: list[CopperArea] = []
    layers = list(board.copper_layers or ["F.Cu", "B.Cu"])
    plane = layers[-1] if layers else "B.Cu"  # prefer bottom/inner as plane
    if len(layers) >= 3:
        plane = layers[1]  # In1-style if present

    for lab in config.nets:
        if lab.net_class not in (NetClass.GROUND, NetClass.POWER):
            continue
        pins = board.nets.get(lab.name) or []
        pts: list[tuple[float, float]] = []
        for ref, _pad in pins:
            c = board.components.get(ref)
            if c:
                pts.append((float(c.x_mm), float(c.y_mm)))
        # Include via locations on this net for pour extent
        for v in route.vias:
            if v.net == lab.name:
                pts.append((v.x, v.y))
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0 = max(0.0, min(xs) - margin_mm)
        y0 = max(0.0, min(ys) - margin_mm)
        x1 = min(board.width_mm, max(xs) + margin_mm)
        y1 = min(board.height_mm, max(ys) + margin_mm)
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:
            continue
        outline = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        areas.append(
            CopperArea(
                outline=outline,
                layer=plane if lab.net_class == NetClass.GROUND else layers[0],
                net=lab.name,
                clearance_mm=0.2,
                min_thickness_mm=0.25,
                priority=10 if lab.net_class == NetClass.GROUND else 5,
            )
        )
    return areas


def apply_feedback_to_config(
    config: PlacementConfig,
    feedback: PhysicsFeedback,
) -> PlacementConfig:
    """Return a copy of config with net weights / physics knobs bumped by feedback."""
    cfg = copy.deepcopy(config)
    by = cfg.net_by_name()
    for net, bump in feedback.placement_weight_bumps.items():
        lab = by.get(net)
        if lab is None:
            continue
        lab.weight = float(lab.weight) + float(bump)
    # Physics term weights
    if feedback.openems_cost > 15:
        cfg.physics.emi_proxy = float(cfg.physics.emi_proxy) * 1.15
        cfg.physics.openems_score = float(cfg.physics.openems_score) * 1.2
    if feedback.spice_cost > 10:
        cfg.physics.spice_score = float(cfg.physics.spice_score) * 1.15
        cfg.physics.power_loop_area = float(cfg.physics.power_loop_area) * 1.1
        cfg.physics.loop_inductance = float(cfg.physics.loop_inductance) * 1.1
    # Matrix skew from raw
    mx = float((feedback.raw.get("matrix_skew_cost") or 0))
    if mx > 5:
        cfg.physics.matrix_length_match = float(cfg.physics.matrix_length_match) * 1.25
    cfg.use_spice = True
    cfg.use_openems = True
    return cfg


def merge_pours_into_route(route: RouteResult, areas: list[CopperArea]) -> RouteResult:
    """Append proposed pours (same-net replace by net+layer)."""
    if not areas:
        return route
    existing = {(a.net, a.layer): a for a in (route.areas or [])}
    for a in areas:
        existing[(a.net, a.layer)] = a
    route.areas = list(existing.values())
    route.notes = list(route.notes or []) + [
        f"physics_feedback: +{len(areas)} pour proposal(s)"
    ]
    return route


def score_full_route_physics(
    board: BoardModel,
    config: PlacementConfig,
    route: RouteResult,
    *,
    export_dir: Path | str | None = None,
    require_complete: bool = True,
    generate_pours: bool = True,
) -> PhysicsFeedback:
    """Run SPICE + OpenEMS proxies on a fully routed board and emit feedback.

    When ``export_dir`` is set and openEMS is installed, also write an OpenEMS
    bundle for offline FDTD (does not block the loop).
    """
    ok, reason = route_is_physics_eligible(
        route, board, require_complete=require_complete
    )
    if not ok:
        return PhysicsFeedback(
            eligible=False,
            reason=reason,
            notes=[f"physics feedback skipped: {reason}"],
        )

    cfg = copy.deepcopy(config)
    cfg.use_spice = True
    cfg.use_openems = True

    sb = geometric_score(board, cfg)
    sb = apply_simulation_scores(
        board, cfg, sb, spice=GeometricSpiceProxy(), openems=OpenEMSBackend()
    )
    spice_c, spice_n = route_aware_spice_cost(board, cfg, route)
    em_c, em_n = route_aware_openems_cost(board, cfg, route)
    mx_c, mx_n = matrix_length_match_score(board, cfg)
    rp_c, rp_n = return_path_score(board, cfg)
    loop = power_loop_area(board, cfg)
    emi = emi_proxy(board, cfg)

    combined = 0.45 * spice_c + 0.45 * em_c + 0.1 * float(sb.total)

    fb = PhysicsFeedback(
        eligible=True,
        reason="ok",
        spice_cost=spice_c,
        openems_cost=em_c,
        combined_cost=combined,
        geometric_total=float(sb.total),
        notes=[
            spice_n,
            em_n,
            mx_n,
            rp_n,
            f"loop_area={loop:.1f}",
            f"emi_proxy={emi:.2f}",
            *list(sb.notes or [])[:4],
        ],
        raw={
            "matrix_skew_cost": mx_c,
            "return_path_cost": rp_c,
            "power_loop_area": loop,
            "emi_proxy": emi,
            "geometric": sb.model_dump() if hasattr(sb, "model_dump") else {},
        },
    )

    # --- Placement weight bumps (next SA round) ---
    for lab in cfg.nets:
        if lab.emi_sensitive or lab.net_class in (NetClass.RF, NetClass.CLOCK):
            if em_c > 12:
                fb.placement_weight_bumps[lab.name] = (
                    fb.placement_weight_bumps.get(lab.name, 0.0) + 0.35
                )
        if lab.net_class in (NetClass.POWER, NetClass.GROUND):
            if spice_c > 8 or loop > 200:
                fb.placement_weight_bumps[lab.name] = (
                    fb.placement_weight_bumps.get(lab.name, 0.0) + 0.25
                )

    # --- Topology hints ---
    if mx_c > 5:
        fb.topology_hints.append("overflow_steiner+matrix_match: reduce CPX skew")
    if em_c > 15:
        fb.topology_hints.append("section_layer: keep EMI nets over continuous reference")
    if spice_c > 10:
        fb.topology_hints.append("power_tree: shorter GND/+ rails; prefer pour connectivity")

    # --- Routing hints ---
    vias = _route_via_count_by_net(route)
    if sum(vias.values()) > max(8, len(board.nets)):
        fb.routing_hints.append("via_minimize: excess vias tax SI/SPICE")
    if em_c > 20:
        fb.routing_hints.append("avoid_parallel_emi: separate analog corridors")

    # --- Pours ---
    if generate_pours and (rp_c > 5 or spice_c > 8 or not route.areas):
        pours = propose_power_pours(board, cfg, route)
        fb.copper_areas = pours
        for a in pours:
            fb.pour_actions.append(
                {
                    "action": "propose_pour",
                    "net": a.net,
                    "layer": a.layer,
                    "outline_pts": len(a.outline),
                }
            )
        if pours:
            merge_pours_into_route(route, pours)

    # --- Optional OpenEMS export for offline FDTD ---
    if export_dir is not None:
        try:
            from physics_router.openems_export import export_openems_bundle

            paths = export_openems_bundle(
                Path(export_dir),
                board=board,
                routes=route,
                config=cfg,
            )
            fb.export_paths = {k: str(v) for k, v in (paths or {}).items()}
            fb.notes.append(f"openems_export:{export_dir}")
        except Exception as exc:
            fb.notes.append(f"openems_export_failed:{exc}")

    # Stash on route quality for viewers
    q = dict(route.quality or {})
    q["physics_feedback"] = fb.to_dict()
    route.quality = q
    return fb
