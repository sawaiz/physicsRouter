"""Exact pin-access and offset-via feasibility planning.

Production detailed routers do not invent a via only after a maze search has
already reached a pad.  They enumerate legal access strategies first and make
global routing consume those finite resources.  This module performs that
preflight for PCB pads using the same geometry helpers as the native DRC gate.

The result is deliberately conservative:

* a via annulus may not overlap any pad on any traversed copper layer;
* foreign pads additionally receive electrical clearance;
* drilled pads receive hole-to-hole clearance;
* the complete via disk stays inside Edge.Cuts plus the copper-edge rule; and
* the surface stub from pad to via is clear of foreign pad copper.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from physics_router.design_rules import DesignRules
from physics_router.models import BoardModel
from physics_router.router import (
    _custom_pad_polygons_board,
    _pad_polygon_board,
    _point_polygon_distance,
    _point_seg_dist,
    _segment_polygon_distance,
    fanout_anchor,
    outline_polygon_from_board,
    point_in_polygon,
)


@dataclass(frozen=True)
class AccessSite:
    net: str
    ref: str
    pad: str
    anchor_index: int
    x: float
    y: float
    terminal_layer: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "net": self.net,
            "ref": self.ref,
            "pad": self.pad,
            "anchor_index": self.anchor_index,
            "x": round(self.x, 4),
            "y": round(self.y, 4),
            "terminal_layer": self.terminal_layer,
            "score": round(self.score, 4),
        }


@dataclass
class PadAccess:
    net: str
    ref: str
    pad: str
    anchor_index: int
    anchor: tuple[float, float]
    layers: tuple[str, ...]
    candidates: list[AccessSite] = field(default_factory=list)
    reason: str = ""

    @property
    def inner_reachable(self) -> bool:
        return bool(self.candidates)


@dataclass
class PinAccessPlan:
    by_net: dict[str, list[PadAccess]]
    via_diameter_mm: float
    via_drill_mm: float
    clearance_mm: float
    metrics: dict[str, Any] = field(default_factory=dict)

    def sites_for(self, net: str, anchor_index: int) -> list[tuple[float, float]]:
        values = self.by_net.get(net) or []
        for value in values:
            if value.anchor_index == anchor_index:
                return [(site.x, site.y) for site in value.candidates]
        return []

    def has_inner_access(self, net: str, anchor_index: int) -> bool:
        return bool(self.sites_for(net, anchor_index))

    def to_dict(self) -> dict[str, Any]:
        return {
            "via_diameter_mm": self.via_diameter_mm,
            "via_drill_mm": self.via_drill_mm,
            "clearance_mm": self.clearance_mm,
            "metrics": dict(self.metrics),
            "pads": {
                net: [
                    {
                        "ref": value.ref,
                        "pad": value.pad,
                        "anchor_index": value.anchor_index,
                        "anchor": [round(value.anchor[0], 4), round(value.anchor[1], 4)],
                        "layers": list(value.layers),
                        "inner_reachable": value.inner_reachable,
                        "reason": value.reason,
                        "candidates": [site.to_dict() for site in value.candidates],
                    }
                    for value in values
                ]
                for net, values in self.by_net.items()
            },
        }


@dataclass(frozen=True)
class _PadGeometry:
    net: str
    ref: str
    pad: str
    center: tuple[float, float]
    layers: frozenset[str]
    polygons: tuple[tuple[tuple[float, float], ...], ...]
    drill_mm: float


def _copper_layers(raw: list[Any], board_layers: list[str]) -> frozenset[str]:
    names = {str(layer) for layer in raw}
    if "*.Cu" in names or "F&B.Cu" in names:
        return frozenset(board_layers)
    return frozenset(layer for layer in board_layers if layer in names)


def _pad_geometries(board: BoardModel) -> list[_PadGeometry]:
    from physics_router.kicad_io import local_to_board

    layers = list(board.copper_layers or ["F.Cu", "B.Cu"])
    result: list[_PadGeometry] = []
    for ref, component in board.components.items():
        for pad in component.pads or []:
            exposed = _copper_layers(list(pad.get("layers") or []), layers)
            if not exposed:
                continue
            center = local_to_board(
                component.x_mm,
                component.y_mm,
                component.rotation_deg,
                float(pad.get("x") or 0.0),
                float(pad.get("y") or 0.0),
            )
            polygons = [_pad_polygon_board(component, pad)]
            polygons.extend(_custom_pad_polygons_board(component, pad))
            result.append(
                _PadGeometry(
                    net=str(pad.get("net") or f"<no-net:{ref}.{pad.get('num', '?')}>") ,
                    ref=ref,
                    pad=str(pad.get("num") or "?"),
                    center=center,
                    layers=exposed,
                    polygons=tuple(tuple(point for point in polygon) for polygon in polygons),
                    drill_mm=abs(float(pad.get("drill") or 0.0)),
                )
            )
    return result


def _outline_edge_distance(
    point: tuple[float, float], outline: list[tuple[float, float]]
) -> float:
    if len(outline) < 3:
        return math.inf
    return min(
        _point_seg_dist(
            point[0],
            point[1],
            outline[index][0],
            outline[index][1],
            outline[(index + 1) % len(outline)][0],
            outline[(index + 1) % len(outline)][1],
        )
        for index in range(len(outline))
    )


def _direction_order(
    anchor: tuple[float, float], center: tuple[float, float], count: int = 24
) -> list[float]:
    outward = math.atan2(anchor[1] - center[1], anchor[0] - center[0])
    base = [2.0 * math.pi * index / count for index in range(count)]
    return sorted(
        base,
        key=lambda value: (
            abs(math.atan2(math.sin(value - outward), math.cos(value - outward))),
            value,
        ),
    )


def _package_kind(component: Any) -> str:
    """Classify footprint for fanout density: bga | qfn | generic."""
    name = str(getattr(component, "footprint", "") or "").lower()
    pads = list(getattr(component, "pads", None) or [])
    n_pads = len(pads)
    if re_search_bga(name) or n_pads >= 64:
        # dense ball-grid or high pin count → BGA-style ring escape
        return "bga"
    if re_search_qfn(name) or (
        n_pads >= 16
        and any(
            abs(float(p.get("x") or 0)) < 0.15 and abs(float(p.get("y") or 0)) < 0.15
            for p in pads
        )
    ):
        return "qfn"
    return "generic"


def re_search_bga(name: str) -> bool:
    import re

    return bool(re.search(r"bga|csp|wlcsp|fbga|ubga|lga", name, re.I))


def re_search_qfn(name: str) -> bool:
    import re

    return bool(re.search(r"qfn|dfn|qfp|mlf|vqfn|wson|son\b", name, re.I))


def _dense_radii(pad_extent: float, via_radius: float, kind: str) -> list[float]:
    start = pad_extent + via_radius + (0.01 if kind == "bga" else 0.02)
    if kind == "bga":
        # Dogbone-ish steps matching typical 0.8/0.65 mm pitch channels
        steps = (0.0, 0.08, 0.16, 0.28, 0.42, 0.58, 0.78, 1.05, 1.35, 1.75, 2.2)
    elif kind == "qfn":
        steps = (0.0, 0.12, 0.25, 0.4, 0.6, 0.85, 1.15, 1.5, 1.9)
    else:
        steps = (0.0, 0.15, 0.3, 0.5, 0.75, 1.05, 1.4, 1.8)
    return [start + s for s in steps]


def _component_center_for_pad(
    board: BoardModel, ref: str, fallback: tuple[float, float]
) -> tuple[float, float]:
    c = board.components.get(ref)
    if c is None:
        return fallback
    return (float(c.x_mm), float(c.y_mm))


def build_pin_access_plan(
    board: BoardModel,
    rules: DesignRules,
    *,
    clearance_mm: float | None = None,
    max_candidates_per_pad: int = 8,
) -> PinAccessPlan:
    """Enumerate legal offset through-via sites for every routed pad.

    Dense packages (BGA / QFN) get tighter radial sampling and escape
    directions ordered outward from the package center (ring/channel fanout).
    """
    layers = list(board.copper_layers or ["F.Cu", "B.Cu"])
    clearance = max(
        rules.constraints.min_clearance_mm,
        float(clearance_mm if clearance_mm is not None else 0.0),
    )
    via_diameter = rules.constraints.min_via_diameter_mm
    via_drill = rules.constraints.min_via_drill_mm
    hole_clearance = rules.constraints.min_hole_to_hole_mm
    edge_clearance = rules.constraints.min_copper_edge_clearance_mm
    via_radius = 0.5 * via_diameter
    track_radius = 0.5 * rules.constraints.min_track_width_mm
    outline = outline_polygon_from_board(board) or []
    pads = _pad_geometries(board)
    center = (0.0, 0.0)
    if board.components:
        center = (
            sum(value.x_mm for value in board.components.values()) / len(board.components),
            sum(value.y_mm for value in board.components.values()) / len(board.components),
        )
    # Per-component package kind cache
    kind_by_ref = {
        ref: _package_kind(comp) for ref, comp in board.components.items()
    }
    dense_refs = {r for r, k in kind_by_ref.items() if k in ("bga", "qfn")}

    by_ref_pad = {(value.ref, value.pad): value for value in pads}
    by_net: dict[str, list[PadAccess]] = {}
    tested = 0
    feasible = 0
    outer_only = 0

    # Match graph/native anchor order exactly: board net order with XY dedup.
    for net in board.nets:
        values: list[PadAccess] = []
        seen: dict[tuple[float, float], int] = {}
        for ref, pad_number in board.nets.get(net) or []:
            component = board.components.get(ref)
            geometry = by_ref_pad.get((ref, str(pad_number)))
            if component is None or geometry is None:
                continue
            anchor = fanout_anchor(board, ref, net, pad_num=str(pad_number))
            key = (round(anchor[0], 3), round(anchor[1], 3))
            if key in seen:
                continue
            anchor_index = len(values)
            seen[key] = anchor_index
            access = PadAccess(
                net=net,
                ref=ref,
                pad=str(pad_number),
                anchor_index=anchor_index,
                anchor=anchor,
                layers=tuple(layer for layer in layers if layer in geometry.layers),
            )
            # Through-hole pads already expose every traversed layer and do not
            # need a separate via access resource.
            if len(geometry.layers) == len(layers):
                access.reason = "pad already spans all copper layers"
                values.append(access)
                continue

            tested += 1
            raw_pad = next(
                (
                    value
                    for value in component.pads or []
                    if str(value.get("num")) == str(pad_number)
                ),
                {},
            )
            pad_extent = 0.5 * max(
                abs(float(raw_pad.get("w") or 0.5)),
                abs(float(raw_pad.get("h") or 0.5)),
            )
            pkg = kind_by_ref.get(ref, "generic")
            radii = _dense_radii(pad_extent, via_radius, pkg)
            # Escape relative to package center for BGA/QFN (ring channels)
            dir_center = (
                _component_center_for_pad(board, ref, center)
                if pkg in ("bga", "qfn")
                else center
            )
            angle_count = 32 if pkg == "bga" else (28 if pkg == "qfn" else 24)
            cand_limit = max_candidates_per_pad + (4 if pkg == "bga" else 0)
            for radius in radii:
                for angle in _direction_order(anchor, dir_center, count=angle_count):
                    site = (
                        anchor[0] + radius * math.cos(angle),
                        anchor[1] + radius * math.sin(angle),
                    )
                    if outline:
                        if not point_in_polygon(site[0], site[1], outline):
                            continue
                        if _outline_edge_distance(site, outline) < via_radius + edge_clearance:
                            continue
                    blocked = False
                    for other in pads:
                        common_layers = set(layers) & set(other.layers)
                        if not common_layers:
                            continue
                        required = via_radius + (0.0 if other.net == net else clearance)
                        if any(
                            _point_polygon_distance(site, list(polygon)) < required - 1e-9
                            for polygon in other.polygons
                        ):
                            blocked = True
                            break
                        if other.drill_mm > 0.0 and math.hypot(
                            site[0] - other.center[0], site[1] - other.center[1]
                        ) < 0.5 * (via_drill + other.drill_mm) + hole_clearance - 1e-9:
                            blocked = True
                            break
                    if blocked:
                        continue

                    terminal_layer = access.layers[0] if access.layers else layers[0]
                    for other in pads:
                        if other.net == net or terminal_layer not in other.layers:
                            continue
                        needed = track_radius + clearance
                        if any(
                            _segment_polygon_distance(anchor, site, list(polygon))
                            < needed - 1e-9
                            for polygon in other.polygons
                        ):
                            blocked = True
                            break
                    if blocked:
                        continue

                    radial_delta = abs(
                        math.atan2(
                            math.sin(angle - math.atan2(anchor[1] - center[1], anchor[0] - center[0])),
                            math.cos(angle - math.atan2(anchor[1] - center[1], anchor[0] - center[0])),
                        )
                    )
                    score = radius + 0.15 * radial_delta
                    # Prefer outer ring escapes slightly for BGA (lower score = better)
                    if pkg == "bga":
                        score = score * 0.85 + 0.05 * radius
                    access.candidates.append(
                        AccessSite(
                            net=net,
                            ref=ref,
                            pad=str(pad_number),
                            anchor_index=anchor_index,
                            x=site[0],
                            y=site[1],
                            terminal_layer=terminal_layer,
                            score=score,
                        )
                    )
                    if len(access.candidates) >= cand_limit:
                        break
                if len(access.candidates) >= cand_limit:
                    break
            access.candidates.sort(key=lambda value: (value.score, value.x, value.y))
            if access.candidates:
                feasible += 1
                access.reason = (
                    f"{len(access.candidates)} legal offset via candidate(s)"
                    + (f" [{pkg} fanout]" if pkg != "generic" else "")
                )
            else:
                outer_only += 1
                access.reason = "no legal through-via escape under active rules"
            values.append(access)
        by_net[net] = values

    return PinAccessPlan(
        by_net=by_net,
        via_diameter_mm=via_diameter,
        via_drill_mm=via_drill,
        clearance_mm=clearance,
        metrics={
            "algorithm": "exact_pad_access_oracle",
            "tested_smd_anchors": tested,
            "inner_reachable_anchors": feasible,
            "outer_only_anchors": outer_only,
            "dense_package_refs": sorted(dense_refs)[:40],
            "bga_qfn_refs": len(dense_refs),
            "candidate_sites": sum(
                len(value.candidates) for values in by_net.values() for value in values
            ),
            "max_candidates_per_pad": max_candidates_per_pad,
        },
    )
