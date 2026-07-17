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
    allow_microvias: bool = False
    allow_blind_buried_vias: bool = False
    board_thickness_mm: float = 1.6


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


def load_design_rules(
    pcb_path: str | Path | None = None,
    pro_path: str | Path | None = None,
) -> DesignRules:
    """Load stackup + DRC from KiCad PCB and/or project files."""
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
    layers: list[StackupLayer] = []
    z = 0.0
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
    total = sum(s.thickness_mm for s in raw) or 1.6
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
