"""Import net labels, weights, and notes from KiCad PCB netclasses + schematics.

Sources:
1. `.kicad_pcb` — `(net_class ...)` / `(add_net ...)` and `(net id "name")`
2. `.kicad_sch` — local/global/hierarchical labels, sheet notes, symbol properties
3. Optional existing YAML merged with import (import fills gaps / overrides by flag)

Heuristic weight map from KiCad netclass name patterns + net name patterns.
Notes are taken from schematic text fields and label shapes when present.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from physics_router.kicad_io import _as_float, _find_all, _find_first, parse_sexpr
from physics_router.models import NetClass, NetLabel, PlacementConfig


# KiCad netclass name → semantic class + default weight
_NETCLASS_MAP: list[tuple[re.Pattern[str], NetClass, float, bool]] = [
    (re.compile(r"power|pwr|vcc|vdd|vbatt", re.I), NetClass.POWER, 4.0, True),
    (re.compile(r"gnd|ground|agnd|dgnd|pgnd", re.I), NetClass.GROUND, 4.0, True),
    (re.compile(r"clk|clock|xtal|osc", re.I), NetClass.CLOCK, 3.5, True),
    (re.compile(r"diff|usb|hdmi|dp|lvds|pcie", re.I), NetClass.DIFFERENTIAL, 3.0, True),
    (re.compile(r"rf|antenna|uhf|vhf", re.I), NetClass.RF, 4.0, True),
    (re.compile(r"analog|adc|audio|sense", re.I), NetClass.ANALOG, 2.5, True),
    (re.compile(r"hs|high.?speed|ddr|mipi", re.I), NetClass.HIGH_SPEED, 3.0, True),
    (re.compile(r"reset|nrst|por", re.I), NetClass.RESET, 2.0, False),
    (re.compile(r"default|signal", re.I), NetClass.SIGNAL, 1.0, False),
]

_NETNAME_MAP: list[tuple[re.Pattern[str], NetClass, float, bool]] = [
    (re.compile(r"^(gnd|ground|agnd|dgnd|vss|pgnd)", re.I), NetClass.GROUND, 4.0, True),
    (re.compile(r"^(\+?\d+\.?\d*v|vcc|vdd|vbat|vin|5v|3v3|1v8|12v)", re.I), NetClass.POWER, 4.0, True),
    (re.compile(r"(^sw$|switch|lx\b|phase)", re.I), NetClass.POWER, 5.0, True),
    (re.compile(r"(clk|clock|xtal|osc|crystal)", re.I), NetClass.CLOCK, 3.5, True),
    (re.compile(r"(usb[_/]?d[pm]|d[pm][_/]?usb|lvds|diff)", re.I), NetClass.DIFFERENTIAL, 3.0, True),
    (re.compile(r"(rf|ant\b|lora|ble)", re.I), NetClass.RF, 4.0, True),
    (re.compile(r"(ain|analog|vref|sense|therm)", re.I), NetClass.ANALOG, 2.5, True),
    (re.compile(r"(reset|nrst|por_)", re.I), NetClass.RESET, 2.0, False),
]


def classify_net(name: str, kicad_netclass: str | None = None) -> tuple[NetClass, float, bool]:
    """Return (semantic_class, weight, critical) heuristics."""
    if kicad_netclass:
        for pat, nc, w, crit in _NETCLASS_MAP:
            if pat.search(kicad_netclass):
                return nc, w, crit
    for pat, nc, w, crit in _NETNAME_MAP:
        if pat.search(name):
            return nc, w, crit
    return NetClass.SIGNAL, 1.0, False


def extract_pcb_netclasses(pcb_path: str | Path) -> dict[str, dict[str, Any]]:
    """Map net_name -> {netclass, clearance, track_width, notes} from .kicad_pcb."""
    text = Path(pcb_path).read_text(encoding="utf-8", errors="replace")
    root = parse_sexpr(text)
    net_to_class: dict[str, str] = {}
    class_meta: dict[str, dict[str, Any]] = {}

    # (net 1 "GND")
    id_to_name: dict[str, str] = {}
    for n in _find_all(root, "net"):
        if len(n) >= 3 and not isinstance(n[1], list):
            id_to_name[str(n[1])] = str(n[2])

    for nc in _find_all(root, "net_class"):
        # (net_class "Power" "note" (clearance 0.2) (trace_width 0.4) (add_net "VCC") ...)
        name = str(nc[1]) if len(nc) > 1 else "Default"
        note = str(nc[2]) if len(nc) > 2 and isinstance(nc[2], str) else ""
        clearance = None
        width = None
        diff_w = None
        diff_gap = None
        cl = _find_first(nc, "clearance")
        if cl and len(cl) >= 2:
            clearance = _as_float(cl[1])
        tw = _find_first(nc, "trace_width") or _find_first(nc, "track_width")
        if tw and len(tw) >= 2:
            width = _as_float(tw[1])
        dw = _find_first(nc, "diff_pair_width")
        if dw and len(dw) >= 2:
            diff_w = _as_float(dw[1])
        dg = _find_first(nc, "diff_pair_gap")
        if dg and len(dg) >= 2:
            diff_gap = _as_float(dg[1])
        class_meta[name] = {
            "notes": note,
            "clearance_mm": clearance,
            "track_width_mm": width,
            "diff_pair_width_mm": diff_w,
            "diff_pair_gap_mm": diff_gap,
        }
        for add in _find_all(nc, "add_net"):
            if len(add) >= 2:
                net_to_class[str(add[1])] = name

    # Also scan (net_class ...) newer format with nested (add_net ...)
    out: dict[str, dict[str, Any]] = {}
    for net_name, cls in net_to_class.items():
        meta = class_meta.get(cls, {})
        out[net_name] = {
            "kicad_netclass": cls,
            "notes": meta.get("notes") or "",
            "clearance_mm": meta.get("clearance_mm"),
            "track_width_mm": meta.get("track_width_mm"),
            "diff_pair_width_mm": meta.get("diff_pair_width_mm"),
            "diff_pair_gap_mm": meta.get("diff_pair_gap_mm"),
        }
    # Nets listed only in (net id name) without class → Default
    for _i, name in id_to_name.items():
        if name and name not in out:
            out[name] = {
                "kicad_netclass": "Default",
                "notes": "",
                "clearance_mm": None,
                "track_width_mm": None,
                "diff_pair_width_mm": None,
                "diff_pair_gap_mm": None,
            }
    return out


_LENGTH_NOTE_RE = re.compile(
    r"(?:max[_\s-]?len(?:gth)?|length\s*[≤<=]|maxlen)\s*[=:]?\s*([\d.]+)\s*(mm|mil)?",
    re.I,
)
_PAIR_NOTE_RE = re.compile(
    r"(?:pair(?:ed)?\s*(?:with|/)|diff(?:erential)?\s*(?:mate|pair)?)\s*[=:]?\s*"
    r"([A-Za-z0-9_+/.-]+)",
    re.I,
)


def _parse_length_mm(note: str) -> float | None:
    m = _LENGTH_NOTE_RE.search(note or "")
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "mm").lower()
    if unit == "mil":
        val *= 0.0254
    return val if val > 0 else None


def _infer_diff_pairs(names: set[str]) -> dict[str, str]:
    """Heuristic mate map for differential naming conventions."""
    pairs: dict[str, str] = {}
    sorted_names = sorted(names)
    for name in sorted_names:
        if name in pairs:
            continue
        mate = None
        # USB D+/D-
        if re.search(r"(d\+|dp|usb_?dp|_p$|_p_)", name, re.I):
            for other in sorted_names:
                if other != name and re.search(r"(d-|dm|usb_?dm|_n$|_n_)", other, re.I):
                    # same stem when possible
                    stem_a = re.sub(r"(d\+|dp|usb_?dp|_p$|_p_)", "", name, flags=re.I)
                    stem_b = re.sub(r"(d-|dm|usb_?dm|_n$|_n_)", "", other, flags=re.I)
                    if stem_a.lower() == stem_b.lower() or abs(len(stem_a) - len(stem_b)) <= 2:
                        mate = other
                        break
            if mate is None:
                for other in sorted_names:
                    if other != name and re.search(r"(d-|dm|usb_?dm|_n$|_n_)", other, re.I):
                        mate = other
                        break
        # _P / _N suffix pairs
        elif re.search(r"_p$", name, re.I):
            cand = re.sub(r"_p$", "_N", name, flags=re.I)
            for other in sorted_names:
                if other.lower() == cand.lower():
                    mate = other
                    break
        if mate:
            pairs[name] = mate
            pairs[mate] = name
    return pairs


def extract_schematic_labels(sch_path: str | Path) -> dict[str, dict[str, Any]]:
    """Extract net-ish labels and notes from a .kicad_sch file."""
    text = Path(sch_path).read_text(encoding="utf-8", errors="replace")
    root = parse_sexpr(text)
    labels: dict[str, dict[str, Any]] = {}

    def add_label(name: str, kind: str, extra_note: str = "") -> None:
        name = name.strip()
        if not name:
            return
        entry = labels.setdefault(name, {"notes": [], "kinds": []})
        if kind not in entry["kinds"]:
            entry["kinds"].append(kind)
        if extra_note:
            entry["notes"].append(extra_note)

    for tag in ("label", "global_label", "hierarchical_label", "netclass_flag"):
        for node in _find_all(root, tag):
            if len(node) < 2:
                continue
            name = str(node[1])
            # shape / fields
            note_parts = []
            shape = _find_first(node, "shape")
            if shape and len(shape) >= 2:
                note_parts.append(f"shape={shape[1]}")
            fields = _find_all(node, "property")
            for prop in fields:
                if len(prop) >= 3:
                    note_parts.append(f"{prop[1]}={prop[2]}")
            add_label(name, tag, "; ".join(str(p) for p in note_parts))

    # Text boxes sometimes document nets: "SW: keep short" — capture SW: notes
    for text_node in _find_all(root, "text") + _find_all(root, "text_box"):
        if len(text_node) < 2:
            continue
        body = str(text_node[1])
        for m in re.finditer(
            r"([A-Za-z0-9_+/.-]+)\s*[:–-]\s*([^\n;]+)",
            body,
        ):
            add_label(m.group(1), "text_note", m.group(2).strip())

    # Normalize notes to string
    for name, entry in labels.items():
        entry["notes"] = " | ".join(entry["notes"]) if entry["notes"] else ""
    return labels


def extract_schematic_dir(project_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Merge labels from all .kicad_sch files under a project directory."""
    project_dir = Path(project_dir)
    merged: dict[str, dict[str, Any]] = {}
    for sch in sorted(project_dir.rglob("*.kicad_sch")):
        part = extract_schematic_labels(sch)
        for name, meta in part.items():
            if name not in merged:
                merged[name] = meta
            else:
                # merge notes
                n1 = merged[name].get("notes") or ""
                n2 = meta.get("notes") or ""
                if n2 and n2 not in n1:
                    merged[name]["notes"] = (n1 + " | " + n2).strip(" |")
                for k in meta.get("kinds", []):
                    if k not in merged[name].setdefault("kinds", []):
                        merged[name]["kinds"].append(k)
    return merged


def build_net_labels(
    pcb_path: str | Path | None = None,
    schematic_path: str | Path | None = None,
    project_dir: str | Path | None = None,
    board_nets: list[str] | None = None,
) -> list[NetLabel]:
    """Build NetLabel list from KiCad sources."""
    pcb_meta: dict[str, dict[str, Any]] = {}
    if pcb_path:
        pcb_meta = extract_pcb_netclasses(pcb_path)

    sch_meta: dict[str, dict[str, Any]] = {}
    if schematic_path:
        sch_meta = extract_schematic_labels(schematic_path)
    elif project_dir:
        sch_meta = extract_schematic_dir(project_dir)

    names: set[str] = set(pcb_meta) | set(sch_meta)
    if board_nets:
        names |= set(board_nets)
    # drop empty net
    names.discard("")

    inferred_pairs = _infer_diff_pairs(names)
    labels: list[NetLabel] = []
    for name in sorted(names):
        meta = pcb_meta.get(name) or {}
        kcls = meta.get("kicad_netclass")
        nc, weight, critical = classify_net(name, kcls)
        notes_parts = []
        if kcls:
            notes_parts.append(f"kicad_netclass={kcls}")
        pnotes = meta.get("notes") or ""
        if pnotes:
            notes_parts.append(str(pnotes))
        snotes = (sch_meta.get(name) or {}).get("notes") or ""
        if snotes:
            notes_parts.append(str(snotes))
        kinds = (sch_meta.get(name) or {}).get("kinds") or []
        if kinds:
            notes_parts.append("sch=" + ",".join(kinds))

        tw = meta.get("track_width_mm")
        cl = meta.get("clearance_mm")
        diff_w = meta.get("diff_pair_width_mm")
        diff_gap = meta.get("diff_pair_gap_mm")
        # power loop grouping for switcher-ish nets
        plg = None
        if re.search(r"(^sw$|switch|lx\b|phase|buck)", name, re.I):
            plg = "auto_switcher"
            critical = True
            weight = max(weight, 5.0)

        pair_with = inferred_pairs.get(name)
        # Explicit pair from schematic notes
        note_blob = " ".join(notes_parts)
        pm = _PAIR_NOTE_RE.search(note_blob)
        if pm:
            cand = pm.group(1).strip()
            if cand in names and cand != name:
                pair_with = cand
        if pair_with:
            nc = NetClass.DIFFERENTIAL
            weight = max(weight, 3.0)
            critical = True

        max_len = _parse_length_mm(note_blob)
        if diff_w and not tw:
            tw = diff_w

        simulate_spice = nc in (NetClass.POWER, NetClass.GROUND) or critical
        simulate_em = nc in (NetClass.RF, NetClass.DIFFERENTIAL, NetClass.HIGH_SPEED) or bool(
            re.search(r"sw", name, re.I)
        )
        emi_sensitive = simulate_em or nc == NetClass.CLOCK

        labels.append(
            NetLabel(
                name=name,
                net_class=nc,
                weight=weight,
                critical=critical,
                pair_with=pair_with,
                max_length_mm=max_len,
                track_width_mm=float(tw) if tw else None,
                clearance_mm=float(cl) if cl else None,
                diff_gap_mm=float(diff_gap) if diff_gap else None,
                power_loop_group=plg,
                simulate_spice=simulate_spice,
                simulate_em=simulate_em,
                emi_sensitive=emi_sensitive,
                notes=" | ".join(notes_parts),
            )
        )
    return labels


def merge_into_config(
    config: PlacementConfig,
    imported: list[NetLabel],
    *,
    override: bool = False,
) -> PlacementConfig:
    """Merge imported nets into config. By default fill missing nets only."""
    existing = {n.name: n for n in config.nets}
    for lab in imported:
        if lab.name not in existing:
            existing[lab.name] = lab
        elif override:
            # keep designer weight if higher?
            old = existing[lab.name]
            existing[lab.name] = lab.model_copy(
                update={
                    "notes": (old.notes + " | " + lab.notes).strip(" |")
                    if old.notes
                    else lab.notes,
                    "weight": max(old.weight, lab.weight),
                    "critical": old.critical or lab.critical,
                }
            )
        else:
            # fill empty notes only
            old = existing[lab.name]
            if not old.notes and lab.notes:
                existing[lab.name] = old.model_copy(update={"notes": lab.notes})
    config.nets = list(existing.values())
    return config


def import_labels_to_config(
    config: PlacementConfig | None = None,
    *,
    pcb_path: str | Path | None = None,
    schematic_path: str | Path | None = None,
    project_dir: str | Path | None = None,
    override: bool = False,
) -> PlacementConfig:
    """High-level: create or update PlacementConfig from KiCad files."""
    from physics_router.kicad_io import load_board_from_kicad_pcb

    cfg = config or PlacementConfig()
    board_nets = None
    if pcb_path:
        board = load_board_from_kicad_pcb(pcb_path, cfg)
        board_nets = list(board.nets.keys())
        if board.width_mm:
            cfg.board_width_mm = board.width_mm
        if board.height_mm:
            cfg.board_height_mm = board.height_mm
    imported = build_net_labels(
        pcb_path=pcb_path,
        schematic_path=schematic_path,
        project_dir=project_dir,
        board_nets=board_nets,
    )
    return merge_into_config(cfg, imported, override=override)
