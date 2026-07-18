"""Export viewer_data.json for the interactive three.js / PCB canvas viewer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from physics_router.models import BoardModel, PlacementConfig
from physics_router.physics import geometric_score, apply_simulation_scores, GeometricSpiceProxy, OpenEMSBackend
from physics_router.router import RouteResult


def board_to_viewer_dict(board: BoardModel, config: PlacementConfig | None = None) -> dict[str, Any]:
    comps = []
    for ref, c in board.components.items():
        item: dict[str, Any] = {
            "ref": ref,
            "x": c.x_mm,
            "y": c.y_mm,
            "rot": c.rotation_deg,
            "w": c.width_mm,
            "h": c.height_mm,
            "layer": c.layer,
            "footprint": c.footprint,
            "locked": c.locked,
            "pads": list(c.pads or []),
        }
        # Real footprint graphics from .kicad_pcb (local coords)
        if c.graphics:
            item["graphics"] = list(c.graphics)
        comps.append(item)
    nets = {n: [{"ref": r, "pad": p} for r, p in pins] for n, pins in board.nets.items()}
    net_meta = {}
    if config:
        for lab in config.nets:
            net_meta[lab.name] = {
                "class": lab.net_class.value if hasattr(lab.net_class, "value") else str(lab.net_class),
                "weight": lab.weight,
                "critical": lab.critical,
                "emi_sensitive": lab.emi_sensitive,
                "power_loop_group": lab.power_loop_group,
                "pair_with": lab.pair_with,
                "notes": lab.notes,
            }
    return {
        "width_mm": board.width_mm,
        "height_mm": board.height_mm,
        "copper_layers": list(board.copper_layers),
        "components": comps,
        "nets": nets,
        "net_meta": net_meta,
        "source_path": board.source_path,
        "outline": list(getattr(board, "outline", None) or []),
    }


def route_to_viewer_dict(route: RouteResult, name: str = "route") -> dict[str, Any]:
    d = route.to_dict() if hasattr(route, "to_dict") else {}
    return {
        "name": name,
        "total_length_mm": route.total_length_mm,
        "via_count": route.via_count,
        "unrouted_nets": route.unrouted_nets,
        "clearance_violations": route.clearance_violations,
        "notes": route.notes,
        "quality": d.get("quality") or (route.compute_quality() if hasattr(route, "compute_quality") else {}),
        "net_reports": d.get("net_reports") or [],
        "length_by_layer_mm": d.get("length_by_layer_mm") or {},
        "segments": [
            {
                "net": s.net,
                "x1": s.x1,
                "y1": s.y1,
                "x2": s.x2,
                "y2": s.y2,
                "layer": s.layer,
                "width_mm": s.width_mm,
            }
            for s in route.segments
        ],
        "vias": [
            {
                "net": v.net,
                "x": v.x,
                "y": v.y,
                "size_mm": v.size_mm,
                "drill_mm": v.drill_mm,
                "layers": list(v.layers),
            }
            for v in route.vias
        ],
    }


def build_viewer_payload(
    board: BoardModel,
    config: PlacementConfig | None = None,
    routes: dict[str, RouteResult] | None = None,
    *,
    include_score: bool = True,
    glb_url: str | None = None,
    step_url: str | None = None,
    emi_geometry_url: str | None = None,
    comparison: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "board": board_to_viewer_dict(board, config),
        "routes": {},
        "assets": {
            "glb": glb_url,
            "step": step_url,
            "emi_geometry": emi_geometry_url,
        },
        "comparison": comparison,
    }
    if routes:
        for name, r in routes.items():
            payload["routes"][name] = route_to_viewer_dict(r, name)
    if include_score and config is not None:
        sb = geometric_score(board, config)
        sb = apply_simulation_scores(
            board, config, sb, spice=GeometricSpiceProxy(), openems=OpenEMSBackend()
        )
        payload["physics"] = {
            "score": sb.as_dict(),
            "notes": sb.notes,
            "weights": config.physics.model_dump() if hasattr(config.physics, "model_dump") else {},
        }
    if extra:
        payload["extra"] = extra
    return payload


def write_viewer_data(payload: dict[str, Any], out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out_path
