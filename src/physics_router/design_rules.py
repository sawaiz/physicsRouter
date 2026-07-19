"""KiCad board design rules, net classes, and stackup.

Loads manufacturing constraints from:
- `.kicad_pcb` — `(layers ...)`, `(setup (stackup ...))`, `(net_class ...)`
- `.kicad_pro` — `board.design_settings` + top-level `net_settings`

These drive clearance, track width, via geometry, and multilayer layer assignment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from physics_router.kicad_io import _as_float, _find_all, _find_first, parse_sexpr
from physics_router.models import NetClass, PlacementConfig


class StackupLayer(BaseModel):
    name: str
    layer_type: str = "copper"  # copper | core | prepreg | mask | paste | silk
    thickness_mm: float = 0.035
    material: str = ""
    epsilon_r: float | None = None
    loss_tangent: float | None = None
    color: str = ""
    z0_mm: float = 0.0  # bottom of layer, computed


class NetClassRules(BaseModel):
    name: str = "Default"
    clearance_mm: float = 0.2
    track_width_mm: float = 0.25
    via_diameter_mm: float = 0.8
    via_drill_mm: float = 0.4
    microvia_diameter_mm: float | None = None
    microvia_drill_mm: float | None = None
    diff_pair_width_mm: float | None = None
    diff_pair_gap_mm: float | None = None
    nets: list[str] = Field(default_factory=list)


class BoardConstraints(BaseModel):
    min_clearance_mm: float = 0.2
    min_track_width_mm: float = 0.15
    min_via_diameter_mm: float = 0.6
    min_via_drill_mm: float = 0.3
    min_via_annular_mm: float = 0.05
    min_copper_edge_clearance_mm: float = 0.3
    min_hole_to_hole_mm: float = 0.25
    # JLCPCB-oriented extras (used by DRC notes / manufacturing check)
    min_via_to_track_mm: float = 0.2
    min_pth_to_track_mm: float = 0.28
    min_solder_mask_bridge_mm: float = 0.1
    min_silk_to_pad_mm: float = 0.15
    min_silk_line_width_mm: float = 0.15
    min_silk_text_height_mm: float = 1.0
    allow_microvias: bool = False
    allow_blind_buried_vias: bool = False
    board_thickness_mm: float = 1.6
    outer_copper_oz: float = 1.0
    inner_copper_oz: float = 0.5
    manufacturer: str = ""
    manufacturer_profile: str = ""


class DesignRules(BaseModel):
    """Unified DRC + stackup model consumed by the router and physics export."""

    copper_layers: list[str] = Field(default_factory=lambda: ["F.Cu", "B.Cu"])
    all_layers: list[str] = Field(default_factory=list)
    stackup: list[StackupLayer] = Field(default_factory=list)
    constraints: BoardConstraints = Field(default_factory=BoardConstraints)
    net_classes: dict[str, NetClassRules] = Field(default_factory=dict)
    # net_name -> net_class name
    net_to_class: dict[str, str] = Field(default_factory=dict)
    track_width_presets_mm: list[float] = Field(default_factory=list)
    via_presets: list[dict[str, float]] = Field(default_factory=list)
    # Preferred signal/power layer roles (heuristic + stackup)
    preferred_signal_layers: list[str] = Field(default_factory=list)
    preferred_plane_layers: list[str] = Field(default_factory=list)
    source_pcb: str | None = None
    source_pro: str | None = None
    notes: list[str] = Field(default_factory=list)

    def clearance_for_net(self, net: str, config: PlacementConfig | None = None) -> float:
        cls = self._class_for(net, config)
        c = max(self.constraints.min_clearance_mm, cls.clearance_mm if cls else 0.2)
        return c

    def track_width_for_net(self, net: str, config: PlacementConfig | None = None) -> float:
        cls = self._class_for(net, config)
        w = cls.track_width_mm if cls else self.constraints.min_track_width_mm
        # Boost power from semantic labels if present
        if config:
            lab = config.net_by_name().get(net)
            if lab and lab.net_class in (NetClass.POWER, NetClass.GROUND):
                w = max(w, 0.3)
                if self.track_width_presets_mm:
                    wider = [p for p in self.track_width_presets_mm if p >= w]
                    if wider:
                        w = min(wider) if lab.net_class == NetClass.GROUND else (
                            wider[min(1, len(wider) - 1)] if len(wider) > 1 else wider[0]
                        )
        return max(w, self.constraints.min_track_width_mm)

    def via_for_net(self, net: str, config: PlacementConfig | None = None) -> tuple[float, float]:
        cls = self._class_for(net, config)
        if cls:
            d, drill = cls.via_diameter_mm, cls.via_drill_mm
        elif self.via_presets:
            d = self.via_presets[0].get("diameter", 0.8)
            drill = self.via_presets[0].get("drill", 0.4)
        else:
            d, drill = 0.8, 0.4
        d = max(d, self.constraints.min_via_diameter_mm)
        return d, drill

    def layers_for_net(self, net: str, config: PlacementConfig | None = None) -> list[str]:
        """Ordered copper layers preferred for this net (primary first)."""
        copper = list(self.copper_layers) or ["F.Cu", "B.Cu"]
        if not config:
            return copper
        lab = config.net_by_name().get(net)
        if lab is None:
            return copper
        # Power/ground prefer plane-capable inner layers when available
        if lab.net_class in (NetClass.POWER, NetClass.GROUND):
            planes = [ly for ly in self.preferred_plane_layers if ly in copper]
            outer = [ly for ly in copper if ly not in planes]
            # Still allow outer for fanout; put planes first for long runs
            return (planes + outer) if planes else copper
        if lab.net_class in (NetClass.ANALOG, NetClass.RF):
            # Prefer outer for controlled geometry / short stubs; avoid noisy digital inners if 4L
            outer = [ly for ly in copper if ly in ("F.Cu", "B.Cu")]
            rest = [ly for ly in copper if ly not in outer]
            return outer + rest
        if lab.net_class in (NetClass.HIGH_SPEED, NetClass.DIFFERENTIAL, NetClass.CLOCK):
            # Prefer layers adjacent to a reference plane
            if len(copper) >= 4:
                # F.Cu / In1 reference, B.Cu / In2 reference style
                return [copper[0], copper[1], copper[-1], *copper[2:-1]]
            return copper
        return copper

    def _class_for(self, net: str, config: PlacementConfig | None) -> NetClassRules | None:
        name = self.net_to_class.get(net)
        if name and name in self.net_classes:
            return self.net_classes[name]
        if "Default" in self.net_classes:
            return self.net_classes["Default"]
        if self.net_classes:
            return next(iter(self.net_classes.values()))
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "copper_layers": self.copper_layers,
            "layer_count": len(self.copper_layers),
            "board_thickness_mm": self.constraints.board_thickness_mm,
            "min_clearance_mm": self.constraints.min_clearance_mm,
            "min_track_width_mm": self.constraints.min_track_width_mm,
            "min_via_diameter_mm": self.constraints.min_via_diameter_mm,
            "min_via_drill_mm": self.constraints.min_via_drill_mm,
            "min_hole_to_hole_mm": self.constraints.min_hole_to_hole_mm,
            "allow_microvias": self.constraints.allow_microvias,
            "allow_blind_buried_vias": self.constraints.allow_blind_buried_vias,
            "net_classes": {
                n: {
                    "clearance_mm": c.clearance_mm,
                    "track_width_mm": c.track_width_mm,
                    "via_diameter_mm": c.via_diameter_mm,
                    "via_drill_mm": c.via_drill_mm,
                    "nets": c.nets,
                }
                for n, c in self.net_classes.items()
            },
            "stackup": [s.model_dump() for s in self.stackup],
            "preferred_signal_layers": self.preferred_signal_layers,
            "preferred_plane_layers": self.preferred_plane_layers,
            "notes": self.notes,
        }


def default_design_rules() -> DesignRules:
    return DesignRules(
        copper_layers=["F.Cu", "B.Cu"],
        preferred_signal_layers=["F.Cu", "B.Cu"],
        preferred_plane_layers=[],
        net_classes={
            "Default": NetClassRules(name="Default"),
        },
        notes=["defaults (no KiCad rules loaded)"],
    )


# Profile id → human metadata (suggestions / limitations for UI)
JLCPCB_PROFILES: dict[str, dict[str, Any]] = {
    "2layer_recommended": {
        "layers": 2,
        "aggressive": False,
        "label": "2-layer recommended",
        "summary": "Cheapest JLC process; good for simple boards and prototypes.",
        "suggestions": [
            "Use pours on B.Cu for GND return under signals",
            "Prefer 0.2 mm track / 0.2 mm clearance for easy DFM",
            "Via 0.6/0.3 mm (pad/drill) avoids special-via surcharges",
            "Keep copper ≥0.3 mm from board outline",
        ],
        "limitations": [
            "No inner planes — higher EMI and harder power distribution",
            "Dense BGA / charlieplex often needs 4L+",
            "Crossings require vias (each via costs impedance discontinuity)",
            "Blind/buried and microvias not supported",
        ],
    },
    "2layer_capability": {
        "layers": 2,
        "aggressive": True,
        "label": "2-layer capability (min)",
        "summary": "Absolute 1 oz 2L fab floors (4/4 mil track/space).",
        "suggestions": [
            "Use only where density requires it; verify DFM in JLC cart",
            "Prefer 0.2 mm via drill when possible (0.15 mm costs more)",
        ],
        "limitations": [
            "Tighter geometries increase scrap risk and cost",
            "Same 2L topology limits as recommended profile",
            "Blind/buried and microvias not supported",
        ],
    },
    "4layer_recommended": {
        "layers": 4,
        "aggressive": False,
        "label": "4-layer recommended (default)",
        "summary": "Standard SIG–GND–PWR–SIG style stack; best default for most boards.",
        "suggestions": [
            "Route signals on F.Cu/B.Cu; keep In1/In2 as plane-like pours",
            "0.15 mm track/space and 0.6/0.3 vias pass free DFM cleanly",
            "Use hybrid free-angle with matrix buses striped across layers",
        ],
        "limitations": [
            "Blind/buried vias not supported (through-hole only)",
            "Microvias not supported",
            "Inner copper default 0.5 oz — current-heavy planes need pours",
        ],
    },
    "4layer_capability": {
        "layers": 4,
        "aggressive": True,
        "label": "4-layer capability (min)",
        "summary": "Multilayer capability floor (3.5/3.5 mil, small vias).",
        "suggestions": [
            "Reserve for BGA fanout or dense LED matrices",
            "Watch annular ring (≥0.15 mm multi absolute min)",
        ],
        "limitations": [
            "Higher cost / DFM flags on tiny vias",
            "Still no blind/buried or microvias",
        ],
    },
    "6layer_recommended": {
        "layers": 6,
        "aggressive": False,
        "label": "6-layer recommended",
        "summary": "Extra planes for HS / mixed-signal; more routing channels.",
        "suggestions": [
            "Typical: SIG–GND–SIG–PWR–GND–SIG (or SIG–GND–SIG–SIG–GND–SIG)",
            "Keep high-speed adjacent to a solid reference plane",
            "Still use through-hole vias only at JLC",
            "Prefer 0.15 mm track/space and 0.6/0.3 vias for easy DFM",
        ],
        "limitations": [
            "Higher cost and longer fab time than 4L",
            "Blind/buried still unsupported — via stubs on thick stacks",
            "Microvias not supported; HDI stacks need other fabs",
            "Impedance control needs JLC impedance calculator + stack notes",
        ],
    },
    "6layer_capability": {
        "layers": 6,
        "aggressive": True,
        "label": "6-layer capability (min)",
        "summary": "Same capability floors as multi-layer, 6 copper layers.",
        "suggestions": [
            "Use only for density; verify stack in JLC impedance tool",
        ],
        "limitations": [
            "Through-hole vias only (stub resonance risk on thick boards)",
            "No microvia / blind-buried HDI",
            "Costly if many small drills",
        ],
    },
}


def list_jlcpcb_profiles() -> list[dict[str, Any]]:
    """UI/API catalog of selectable JLCPCB fab profiles."""
    out: list[dict[str, Any]] = []
    for pid, meta in JLCPCB_PROFILES.items():
        out.append(
            {
                "id": pid,
                "layers": meta["layers"],
                "aggressive": meta["aggressive"],
                "label": meta["label"],
                "summary": meta["summary"],
                "suggestions": list(meta["suggestions"]),
                "limitations": list(meta["limitations"]),
            }
        )
    return out


def _jlc_stackup(layers: int) -> tuple[list[str], list[StackupLayer]]:
    """Approximate FR-4 stack for physics export (not a fab construction sheet)."""
    outer = 0.035  # ~1 oz
    inner = 0.0175  # ~0.5 oz
    if layers == 2:
        copper = ["F.Cu", "B.Cu"]
        stackup = [
            StackupLayer(name="F.Cu", layer_type="copper", thickness_mm=outer, material="Cu"),
            StackupLayer(
                name="dielectric_core",
                layer_type="core",
                thickness_mm=1.53,
                material="FR4",
                epsilon_r=4.5,
            ),
            StackupLayer(name="B.Cu", layer_type="copper", thickness_mm=outer, material="Cu"),
        ]
    elif layers == 6:
        copper = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
        # SIG GND SIG PWR GND SIG style thicknesses (approximate)
        stackup = [
            StackupLayer(name="F.Cu", layer_type="copper", thickness_mm=outer, material="Cu"),
            StackupLayer(name="pp1", layer_type="prepreg", thickness_mm=0.12, material="FR4", epsilon_r=4.4),
            StackupLayer(name="In1.Cu", layer_type="copper", thickness_mm=inner, material="Cu"),
            StackupLayer(name="core1", layer_type="core", thickness_mm=0.3, material="FR4", epsilon_r=4.5),
            StackupLayer(name="In2.Cu", layer_type="copper", thickness_mm=inner, material="Cu"),
            StackupLayer(name="pp2", layer_type="prepreg", thickness_mm=0.2, material="FR4", epsilon_r=4.4),
            StackupLayer(name="In3.Cu", layer_type="copper", thickness_mm=inner, material="Cu"),
            StackupLayer(name="core2", layer_type="core", thickness_mm=0.3, material="FR4", epsilon_r=4.5),
            StackupLayer(name="In4.Cu", layer_type="copper", thickness_mm=inner, material="Cu"),
            StackupLayer(name="pp3", layer_type="prepreg", thickness_mm=0.12, material="FR4", epsilon_r=4.4),
            StackupLayer(name="B.Cu", layer_type="copper", thickness_mm=outer, material="Cu"),
        ]
    else:
        # 4L default
        copper = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        stackup = [
            StackupLayer(name="F.Cu", layer_type="copper", thickness_mm=outer, material="Cu"),
            StackupLayer(name="dielectric_prepreg1", layer_type="prepreg", thickness_mm=0.2, material="FR4", epsilon_r=4.4),
            StackupLayer(name="In1.Cu", layer_type="copper", thickness_mm=inner, material="Cu"),
            StackupLayer(name="dielectric_core", layer_type="core", thickness_mm=1.1, material="FR4", epsilon_r=4.5),
            StackupLayer(name="In2.Cu", layer_type="copper", thickness_mm=inner, material="Cu"),
            StackupLayer(name="dielectric_prepreg2", layer_type="prepreg", thickness_mm=0.2, material="FR4", epsilon_r=4.4),
            StackupLayer(name="B.Cu", layer_type="copper", thickness_mm=outer, material="Cu"),
        ]
    z = 0.0
    for ly in reversed(stackup):
        ly.z0_mm = z
        z += ly.thickness_mm
    return copper, stackup


def jlcpcb_design_rules(
    *,
    layers: int = 4,
    aggressive: bool = False,
) -> DesignRules:
    """JLCPCB FR-4 manufacturing profile for 2 / 4 / 6 copper layers.

    Values follow JLCPCB published capabilities:
    https://jlcpcb.com/capabilities/pcb-capabilities

    * ``aggressive=False`` — production-friendly (recommended) floors.
    * ``aggressive=True`` — absolute capability floors.
    Blind/buried and microvias are always disabled (unsupported at JLCPCB).
    """
    if layers not in (2, 4, 6):
        raise ValueError("layers must be 2, 4, or 6")

    multi = layers >= 4
    if aggressive:
        # Capability: 2L 0.10/0.10; multi 0.09/0.09; via multi 0.25/0.15 abs, prefer 0.45/0.2
        track = 0.10 if layers == 2 else 0.09
        c = BoardConstraints(
            min_clearance_mm=track,
            min_track_width_mm=track,
            min_via_diameter_mm=0.45 if multi else 0.45,
            min_via_drill_mm=0.2,
            min_via_annular_mm=0.15 if multi else 0.18,
            min_copper_edge_clearance_mm=0.2,
            min_hole_to_hole_mm=0.2,
            min_via_to_track_mm=0.2,
            min_pth_to_track_mm=0.28,
            min_solder_mask_bridge_mm=0.1,
            min_silk_to_pad_mm=0.15,
            min_silk_line_width_mm=0.15,
            min_silk_text_height_mm=1.0,
            allow_microvias=False,
            allow_blind_buried_vias=False,
            board_thickness_mm=1.6,
            outer_copper_oz=1.0,
            inner_copper_oz=0.5 if multi else 0.0,
            manufacturer="JLCPCB",
            manufacturer_profile=f"{layers}layer_capability",
        )
        default_w, default_cl = 0.127, 0.127
        via_d, via_drill = 0.45, 0.2
    else:
        c = BoardConstraints(
            min_clearance_mm=0.15 if multi else 0.2,
            min_track_width_mm=0.15 if multi else 0.2,
            min_via_diameter_mm=0.6,
            min_via_drill_mm=0.3,
            min_via_annular_mm=0.15 if multi else 0.2,
            min_copper_edge_clearance_mm=0.3,
            min_hole_to_hole_mm=0.45,
            min_via_to_track_mm=0.2,
            min_pth_to_track_mm=0.35,
            min_solder_mask_bridge_mm=0.1,
            min_silk_to_pad_mm=0.15,
            min_silk_line_width_mm=0.15,
            min_silk_text_height_mm=1.0,
            allow_microvias=False,
            allow_blind_buried_vias=False,
            board_thickness_mm=1.6,
            outer_copper_oz=1.0,
            inner_copper_oz=0.5 if multi else 0.0,
            manufacturer="JLCPCB",
            manufacturer_profile=f"{layers}layer_recommended",
        )
        default_w = 0.2 if layers == 2 else 0.2
        default_cl = 0.2 if layers == 2 else 0.15
        via_d, via_drill = 0.6, 0.3

    copper, stackup = _jlc_stackup(layers)
    if layers == 2:
        sig, plane = ["F.Cu", "B.Cu"], ["B.Cu"]
    elif layers == 6:
        # Prefer outer + mid signal; inners 1/4 as GND-like, 3 as PWR-like
        sig = ["F.Cu", "In2.Cu", "In3.Cu", "B.Cu"]
        plane = ["In1.Cu", "In4.Cu", "In3.Cu"]
    else:
        sig, plane = ["F.Cu", "B.Cu"], ["In1.Cu", "In2.Cu"]

    meta = JLCPCB_PROFILES.get(c.manufacturer_profile, {})
    rules = DesignRules(
        copper_layers=copper,
        all_layers=copper
        + ["F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts", "F.Paste", "B.Paste"],
        stackup=stackup,
        constraints=c,
        track_width_presets_mm=[0.1, 0.127, 0.15, 0.2, 0.25, 0.3, 0.5, 0.8, 1.0],
        via_presets=[
            {"diameter": via_d, "drill": via_drill},
            {"diameter": 0.45, "drill": 0.2},
            {"diameter": 0.8, "drill": 0.4},
        ],
        preferred_signal_layers=sig,
        preferred_plane_layers=plane,
        net_classes={
            "Default": NetClassRules(
                name="Default",
                clearance_mm=default_cl,
                track_width_mm=default_w,
                via_diameter_mm=via_d,
                via_drill_mm=via_drill,
            ),
            "Power": NetClassRules(
                name="Power",
                clearance_mm=max(default_cl, 0.2),
                track_width_mm=max(default_w, 0.5 if layers == 2 else 0.4),
                via_diameter_mm=max(via_d, 0.6),
                via_drill_mm=max(via_drill, 0.3),
            ),
            "Ground": NetClassRules(
                name="Ground",
                clearance_mm=max(default_cl, 0.2),
                track_width_mm=max(default_w, 0.3),
                via_diameter_mm=max(via_d, 0.6),
                via_drill_mm=max(via_drill, 0.3),
            ),
            "Signal": NetClassRules(
                name="Signal",
                clearance_mm=default_cl,
                track_width_mm=default_w,
                via_diameter_mm=via_d,
                via_drill_mm=via_drill,
            ),
        },
        notes=[
            f"JLCPCB {layers}-layer FR-4 profile (through-hole vias only)",
            f"profile={c.manufacturer_profile} thickness={c.board_thickness_mm}mm "
            f"outer={c.outer_copper_oz}oz inner={c.inner_copper_oz}oz",
            f"DRC floors: track≥{c.min_track_width_mm}mm clearance≥{c.min_clearance_mm}mm "
            f"via≥{c.min_via_diameter_mm}/{c.min_via_drill_mm}mm "
            f"edge≥{c.min_copper_edge_clearance_mm}mm",
            "ERC: Power/Ground net classes; no microvias/blind/buried",
            f"suggestions: {'; '.join(meta.get('suggestions', [])[:2])}",
            f"limitations: {'; '.join(meta.get('limitations', [])[:2])}",
        ],
    )
    _finalize_layer_roles(rules)
    return rules


def jlcpcb_4layer_design_rules(*, aggressive: bool = False) -> DesignRules:
    """Back-compat wrapper → :func:`jlcpcb_design_rules` with ``layers=4``."""
    return jlcpcb_design_rules(layers=4, aggressive=aggressive)


def jlcpcb_2layer_design_rules(*, aggressive: bool = False) -> DesignRules:
    return jlcpcb_design_rules(layers=2, aggressive=aggressive)


def jlcpcb_6layer_design_rules(*, aggressive: bool = False) -> DesignRules:
    return jlcpcb_design_rules(layers=6, aggressive=aggressive)


def parse_jlc_profile(profile: str | None) -> tuple[int, bool]:
    """Return ``(layers, aggressive)`` for a profile id like ``4layer_recommended``."""
    p = (profile or "4layer_recommended").strip().lower().replace(" ", "").replace("-", "")
    # aliases
    aliases = {
        "2l": "2layer_recommended",
        "2layer": "2layer_recommended",
        "4l": "4layer_recommended",
        "4layer": "4layer_recommended",
        "6l": "6layer_recommended",
        "6layer": "6layer_recommended",
        "2lmin": "2layer_capability",
        "4lmin": "4layer_capability",
        "6lmin": "6layer_capability",
    }
    p = aliases.get(p, p)
    if p not in JLCPCB_PROFILES:
        # try Nlayer_recommended
        if p.endswith("layer"):
            p = p + "_recommended"
    if p not in JLCPCB_PROFILES:
        p = "4layer_recommended"
    meta = JLCPCB_PROFILES[p]
    return int(meta["layers"]), bool(meta["aggressive"])


def apply_manufacturer_floors(
    rules: DesignRules,
    *,
    manufacturer: str = "JLCPCB",
    profile: str = "4layer_recommended",
) -> DesignRules:
    """Raise rule floors so they never undercut the manufacturer profile.

    Existing KiCad project rules that are *tighter* (larger mins) are kept.
    Copper layer list is only replaced when the board has fewer layers than the
    selected profile (e.g. synthetic 2L board upgraded to 4L template).
    """
    if manufacturer.upper() not in ("JLC", "JLCPCB"):
        return rules

    layers, aggressive = parse_jlc_profile(profile)
    mfg = jlcpcb_design_rules(layers=layers, aggressive=aggressive)

    c, m = rules.constraints, mfg.constraints
    # Take the max of each geometric floor (more conservative for fab)
    c.min_clearance_mm = max(c.min_clearance_mm, m.min_clearance_mm)
    c.min_track_width_mm = max(c.min_track_width_mm, m.min_track_width_mm)
    c.min_via_diameter_mm = max(c.min_via_diameter_mm, m.min_via_diameter_mm)
    c.min_via_drill_mm = max(c.min_via_drill_mm, m.min_via_drill_mm)
    c.min_via_annular_mm = max(c.min_via_annular_mm, m.min_via_annular_mm)
    c.min_copper_edge_clearance_mm = max(
        c.min_copper_edge_clearance_mm, m.min_copper_edge_clearance_mm
    )
    c.min_hole_to_hole_mm = max(c.min_hole_to_hole_mm, m.min_hole_to_hole_mm)
    c.min_via_to_track_mm = max(
        getattr(c, "min_via_to_track_mm", 0) or 0, m.min_via_to_track_mm
    )
    c.min_pth_to_track_mm = max(
        getattr(c, "min_pth_to_track_mm", 0) or 0, m.min_pth_to_track_mm
    )
    c.min_solder_mask_bridge_mm = max(
        getattr(c, "min_solder_mask_bridge_mm", 0) or 0, m.min_solder_mask_bridge_mm
    )
    c.min_silk_to_pad_mm = max(
        getattr(c, "min_silk_to_pad_mm", 0) or 0, m.min_silk_to_pad_mm
    )
    c.min_silk_line_width_mm = max(
        getattr(c, "min_silk_line_width_mm", 0) or 0, m.min_silk_line_width_mm
    )
    c.min_silk_text_height_mm = max(
        getattr(c, "min_silk_text_height_mm", 0) or 0, m.min_silk_text_height_mm
    )
    # JLC never allows blind/buried/microvias on standard process
    c.allow_microvias = False
    c.allow_blind_buried_vias = False
    c.manufacturer = m.manufacturer
    c.manufacturer_profile = m.manufacturer_profile
    if not c.board_thickness_mm or c.board_thickness_mm < 0.4:
        c.board_thickness_mm = m.board_thickness_mm
    c.outer_copper_oz = m.outer_copper_oz
    c.inner_copper_oz = m.inner_copper_oz

    # Ensure net classes meet floors
    for nc in rules.net_classes.values():
        nc.clearance_mm = max(nc.clearance_mm, c.min_clearance_mm)
        nc.track_width_mm = max(nc.track_width_mm, c.min_track_width_mm)
        nc.via_diameter_mm = max(nc.via_diameter_mm, c.min_via_diameter_mm)
        nc.via_drill_mm = max(nc.via_drill_mm, c.min_via_drill_mm)

    # Adopt profile copper list when board is thinner than profile
    if len(rules.copper_layers) < layers:
        rules.copper_layers = list(mfg.copper_layers)
        rules.stackup = list(mfg.stackup)
        rules.preferred_signal_layers = list(mfg.preferred_signal_layers)
        rules.preferred_plane_layers = list(mfg.preferred_plane_layers)
        rules.notes.append(
            f"copper stack upgraded to JLCPCB {layers}L template for routing"
        )

    rules.notes = list(rules.notes or [])
    tag = f"manufacturer floors applied: {c.manufacturer} {c.manufacturer_profile}"
    if tag not in rules.notes:
        rules.notes.append(tag)
    meta = JLCPCB_PROFILES.get(c.manufacturer_profile)
    if meta:
        rules.notes.append("suggestions: " + "; ".join(meta["suggestions"][:3]))
        rules.notes.append("limitations: " + "; ".join(meta["limitations"][:3]))
    _finalize_layer_roles(rules)
    return rules


def load_design_rules(
    pcb_path: str | Path | None = None,
    pro_path: str | Path | None = None,
    *,
    manufacturer: str | None = "JLCPCB",
    jlc_profile: str = "4layer_recommended",
) -> DesignRules:
    """Load stackup + DRC from KiCad PCB and/or project files.

    When ``manufacturer`` is JLCPCB (default), rule floors are raised to the
    selected profile (``2layer_*`` / ``4layer_*`` / ``6layer_*``).
    Pass ``manufacturer=None`` to keep the source KiCad project's numbers.
    KiCad validation still copies and uses the source project rules; the
    manufacturing profile is a deliberately more conservative route target.
    """
    rules = default_design_rules()
    if pcb_path:
        pcb_path = Path(pcb_path)
        rules.source_pcb = str(pcb_path)
        _merge_from_pcb(rules, pcb_path)
        if pro_path is None:
            # sibling .kicad_pro
            cand = pcb_path.with_suffix(".kicad_pro")
            if cand.exists():
                pro_path = cand
    if pro_path:
        pro_path = Path(pro_path)
        rules.source_pro = str(pro_path)
        _merge_from_pro(rules, pro_path)

    _finalize_layer_roles(rules)

    if manufacturer and manufacturer.upper() in ("JLC", "JLCPCB"):
        layers_req, aggressive = parse_jlc_profile(jlc_profile)
        # Auto-pick profile from board layer count when caller left default 4L
        # but board is clearly 2L or 6L
        n_cu = len(rules.copper_layers)
        if jlc_profile in ("4layer_recommended", "4layer") and n_cu in (2, 6):
            jlc_profile = f"{n_cu}layer_recommended"
            layers_req, aggressive = parse_jlc_profile(jlc_profile)
        # If project already uses sub-0.12 mm geometry, allow capability floors
        if (
            not aggressive
            and rules.constraints.min_track_width_mm < 0.12
            and "capability" not in jlc_profile
        ):
            jlc_profile = f"{layers_req}layer_capability"
        rules = apply_manufacturer_floors(
            rules, manufacturer="JLCPCB", profile=jlc_profile
        )
    return rules


def _merge_from_pcb(rules: DesignRules, path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    root = parse_sexpr(text)

    # Copper layers from (layers ...)
    layers_node = _find_first(root, "layers")
    copper: list[str] = []
    all_ly: list[str] = []
    if layers_node:
        for child in layers_node[1:]:
            if not isinstance(child, list) or len(child) < 3:
                continue
            # (0 "F.Cu" signal) or (0 "F.Cu" signal "Front")
            name = str(child[1]).strip('"')
            kind = str(child[2]) if len(child) > 2 else ""
            all_ly.append(name)
            if kind == "signal" or name.endswith(".Cu"):
                if name.endswith(".Cu"):
                    copper.append(name)
    if copper:
        rules.copper_layers = copper
    rules.all_layers = all_ly

    # Board thickness
    general = _find_first(root, "general")
    if general:
        th = _find_first(general, "thickness")
        if th and len(th) >= 2:
            rules.constraints.board_thickness_mm = _as_float(th[1], 1.6)

    # Stackup
    setup = _find_first(root, "setup")
    if setup:
        stack = _find_first(setup, "stackup")
        if stack:
            rules.stackup = _parse_stackup(stack)

    # PCB-embedded net_class blocks (legacy / some versions)
    for nc in _find_all(root, "net_class"):
        name = str(nc[1]) if len(nc) > 1 else "Default"
        clearance = rules.constraints.min_clearance_mm
        width = rules.constraints.min_track_width_mm
        via_d = rules.constraints.min_via_diameter_mm
        via_drill = 0.3
        nets: list[str] = []
        cl = _find_first(nc, "clearance")
        if cl and len(cl) >= 2:
            clearance = _as_float(cl[1], clearance)
        tw = _find_first(nc, "trace_width") or _find_first(nc, "track_width")
        if tw and len(tw) >= 2:
            width = _as_float(tw[1], width)
        vd = _find_first(nc, "via_dia") or _find_first(nc, "via_diameter")
        if vd and len(vd) >= 2:
            via_d = _as_float(vd[1], via_d)
        vdr = _find_first(nc, "via_drill")
        if vdr and len(vdr) >= 2:
            via_drill = _as_float(vdr[1], via_drill)
        for add in _find_all(nc, "add_net"):
            if len(add) >= 2:
                n = str(add[1])
                nets.append(n)
                rules.net_to_class[n] = name
        rules.net_classes[name] = NetClassRules(
            name=name,
            clearance_mm=clearance,
            track_width_mm=width,
            via_diameter_mm=via_d,
            via_drill_mm=via_drill,
            nets=nets,
        )
    rules.notes.append(f"loaded stackup/layers from {path.name}")


def _parse_stackup(stack_node: list[Any]) -> list[StackupLayer]:
    # Walk from bottom-ish: KiCad lists top-to-bottom in file often F.Silk first
    # We accumulate thickness top→bottom then reassign z0 from bottom.
    raw: list[StackupLayer] = []
    for child in stack_node[1:]:
        if not isinstance(child, list) or not child:
            continue
        if child[0] != "layer":
            continue
        name = str(child[1]) if len(child) > 1 else ""
        ltype = ""
        thickness = 0.0
        material = ""
        er = None
        lt = None
        color = ""
        for sub in child[2:]:
            if not isinstance(sub, list) or not sub:
                continue
            if sub[0] == "type" and len(sub) >= 2:
                ltype = str(sub[1])
            elif sub[0] == "thickness" and len(sub) >= 2:
                thickness = _as_float(sub[1])
            elif sub[0] == "material" and len(sub) >= 2:
                material = str(sub[1])
            elif sub[0] == "epsilon_r" and len(sub) >= 2:
                er = _as_float(sub[1])
            elif sub[0] == "loss_tangent" and len(sub) >= 2:
                lt = _as_float(sub[1])
            elif sub[0] == "color" and len(sub) >= 2:
                color = str(sub[1])
        # normalize type
        lt_norm = ltype.lower()
        if "copper" in lt_norm:
            kind = "copper"
        elif "core" in lt_norm:
            kind = "core"
        elif "prepreg" in lt_norm:
            kind = "prepreg"
        elif "mask" in lt_norm:
            kind = "mask"
        elif "paste" in lt_norm:
            kind = "paste"
        elif "silk" in lt_norm:
            kind = "silk"
        else:
            kind = ltype or "other"
        raw.append(
            StackupLayer(
                name=name,
                layer_type=kind,
                thickness_mm=thickness,
                material=material,
                epsilon_r=er,
                loss_tangent=lt,
                color=color,
            )
        )
    # KiCad stackup is typically listed top→bottom; set z0 from bottom of board
    # z=0 at bottom copper underside
    z = 0.0
    # reverse: build bottom-up
    for s in reversed(raw):
        s.z0_mm = z
        z += s.thickness_mm
    # return original top→bottom order with z0 set
    return raw


def _merge_from_pro(rules: DesignRules, path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    board = data.get("board") or {}
    ds = board.get("design_settings") or {}
    r = ds.get("rules") or {}

    c = rules.constraints
    if "min_clearance" in r:
        c.min_clearance_mm = float(r["min_clearance"])
    if "min_track_width" in r:
        c.min_track_width_mm = float(r["min_track_width"])
    if "min_via_diameter" in r:
        c.min_via_diameter_mm = float(r["min_via_diameter"])
    if "min_through_hole_diameter" in r:
        c.min_via_drill_mm = float(r["min_through_hole_diameter"])
    if "min_via_annular_width" in r:
        c.min_via_annular_mm = float(r["min_via_annular_width"])
    if "min_copper_edge_clearance" in r:
        c.min_copper_edge_clearance_mm = float(r["min_copper_edge_clearance"])
    if "min_hole_to_hole" in r:
        c.min_hole_to_hole_mm = float(r["min_hole_to_hole"])
    if "allow_microvias" in r:
        c.allow_microvias = bool(r["allow_microvias"])
    if "allow_blind_buried_vias" in r:
        c.allow_blind_buried_vias = bool(r["allow_blind_buried_vias"])

    tw = ds.get("track_widths") or []
    if tw:
        rules.track_width_presets_mm = [float(x) for x in tw]
    vias = ds.get("via_dimensions") or []
    if vias:
        rules.via_presets = [
            {"diameter": float(v.get("diameter", 0.8)), "drill": float(v.get("drill", 0.4))}
            for v in vias
        ]

    # Net classes: pro may store under net_settings at root or board
    ns = data.get("net_settings") or {}
    classes = ns.get("classes") or []
    # Also design_settings sometimes duplicates
    if not classes and isinstance(ds.get("net_settings"), dict):
        classes = ds["net_settings"].get("classes") or []

    for cls in classes:
        name = str(cls.get("name", "Default"))
        ncr = NetClassRules(
            name=name,
            clearance_mm=float(cls.get("clearance", c.min_clearance_mm)),
            track_width_mm=float(cls.get("track_width", c.min_track_width_mm)),
            via_diameter_mm=float(cls.get("via_diameter", c.min_via_diameter_mm)),
            via_drill_mm=float(cls.get("via_drill", 0.3)),
            microvia_diameter_mm=_opt_float(cls.get("microvia_diameter")),
            microvia_drill_mm=_opt_float(cls.get("microvia_drill")),
            diff_pair_width_mm=_opt_float(cls.get("diff_pair_width")),
            diff_pair_gap_mm=_opt_float(cls.get("diff_pair_gap")),
            nets=list(cls.get("nets") or []),
        )
        for n in ncr.nets:
            rules.net_to_class[n] = name
        rules.net_classes[name] = ncr

    if not rules.net_classes:
        rules.net_classes["Default"] = NetClassRules(
            name="Default",
            clearance_mm=c.min_clearance_mm,
            track_width_mm=c.min_track_width_mm,
            via_diameter_mm=c.min_via_diameter_mm,
            via_drill_mm=c.min_via_drill_mm,
        )

    rules.notes.append(f"loaded DRC/netclasses from {path.name}")


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _finalize_layer_roles(rules: DesignRules) -> None:
    copper = rules.copper_layers
    if len(copper) >= 4:
        # Classic 4L: SIG / GND / PWR / SIG or SIG / GND / GND / SIG
        rules.preferred_signal_layers = [copper[0], copper[-1]]
        rules.preferred_plane_layers = copper[1:-1]
        rules.notes.append(
            "4+ layer stack: outer layers preferred for signals; "
            "inner copper preferred for power/ground planes"
        )
    elif len(copper) == 2:
        rules.preferred_signal_layers = list(copper)
        rules.preferred_plane_layers = [copper[-1]]  # B.Cu often ground flood
        rules.notes.append("2-layer stack: route with return on opposite layer when possible")
    else:
        rules.preferred_signal_layers = list(copper)
        rules.preferred_plane_layers = copper[1:] if len(copper) > 1 else []

    # Align via min with annular
    c = rules.constraints
    if c.min_via_diameter_mm < c.min_via_drill_mm + 2 * c.min_via_annular_mm:
        c.min_via_diameter_mm = c.min_via_drill_mm + 2 * c.min_via_annular_mm


def apply_rules_to_config(rules: DesignRules, config: PlacementConfig) -> PlacementConfig:
    """Annotate PlacementConfig notes with KiCad DRC summary (non-destructive)."""
    extra = (
        f"KiCad DRC: min_clearance={rules.constraints.min_clearance_mm}mm "
        f"min_track={rules.constraints.min_track_width_mm}mm "
        f"layers={rules.copper_layers} "
        f"thickness={rules.constraints.board_thickness_mm}mm"
    )
    if extra not in config.notes:
        config.notes = (config.notes + " | " + extra).strip(" |")
    return config
