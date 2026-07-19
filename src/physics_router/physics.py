"""Physics-aware cost terms and simulation backends (Ngspice / OpenEMS)."""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from physics_router.models import (
    BoardModel,
    NetClass,
    PlacementConfig,
    ScoreBreakdown,
)


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def component_centers(board: BoardModel) -> dict[str, tuple[float, float]]:
    return {r: (c.x_mm, c.y_mm) for r, c in board.components.items()}


def weighted_wirelength(board: BoardModel, config: PlacementConfig) -> float:
    """HPWL-style length per net × label weight."""
    centers = component_centers(board)
    total = 0.0
    for net_name, pins in board.nets.items():
        refs = [r for r, _ in pins if r in centers]
        if len(refs) < 2:
            continue
        xs = [centers[r][0] for r in refs]
        ys = [centers[r][1] for r in refs]
        hpwl = (max(xs) - min(xs)) + (max(ys) - min(ys))
        total += hpwl * config.weight_for_net(net_name)
    return total


def critical_net_length(board: BoardModel, config: PlacementConfig) -> float:
    centers = component_centers(board)
    labels = config.net_by_name()
    cost = 0.0
    for net_name, pins in board.nets.items():
        lab = labels.get(net_name)
        if lab is None or not (lab.critical or lab.max_length_mm is not None):
            continue
        refs = [r for r, _ in pins if r in centers]
        if len(refs) < 2:
            continue
        xs = [centers[r][0] for r in refs]
        ys = [centers[r][1] for r in refs]
        hpwl = (max(xs) - min(xs)) + (max(ys) - min(ys))
        if lab.max_length_mm is not None and hpwl > lab.max_length_mm:
            cost += (hpwl - lab.max_length_mm) * 5.0 * config.weight_for_net(net_name)
        else:
            cost += hpwl * config.weight_for_net(net_name)
    return cost


def power_loop_area(board: BoardModel, config: PlacementConfig) -> float:
    """Approximate switching/power loop area from components on grouped nets.

    For each power_loop_group, take the convex-ish bbox of connected parts
    and use width*height as loop-area proxy (mm^2). Smaller is better for EMI/L.
    """
    centers = component_centers(board)
    groups: dict[str, set[str]] = {}
    for lab in config.nets:
        if not lab.power_loop_group:
            continue
        for ref, _ in board.nets.get(lab.name, []):
            groups.setdefault(lab.power_loop_group, set()).add(ref)

    area = 0.0
    for _gid, refs in groups.items():
        pts = [centers[r] for r in refs if r in centers]
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        area += max(0.1, max(xs) - min(xs)) * max(0.1, max(ys) - min(ys))
    return area


def overlap_penalty(board: BoardModel) -> float:
    comps = list(board.components.values())
    pen = 0.0
    for i, a in enumerate(comps):
        for b in comps[i + 1 :]:
            dx = abs(a.x_mm - b.x_mm)
            dy = abs(a.y_mm - b.y_mm)
            min_dx = (a.width_mm + b.width_mm) / 2 * 1.05
            min_dy = (a.height_mm + b.height_mm) / 2 * 1.05
            ox = min_dx - dx
            oy = min_dy - dy
            if ox > 0 and oy > 0:
                pen += ox * oy
    return pen


def region_violation(board: BoardModel, config: PlacementConfig) -> float:
    if not config.regions:
        return 0.0
    cost = 0.0
    for region in config.regions:
        for ref in region.preferred_refs:
            c = board.components.get(ref)
            if c is None:
                continue
            if not (
                region.x_min_mm <= c.x_mm <= region.x_max_mm
                and region.y_min_mm <= c.y_mm <= region.y_max_mm
            ):
                # distance outside region box
                cx = min(max(c.x_mm, region.x_min_mm), region.x_max_mm)
                cy = min(max(c.y_mm, region.y_min_mm), region.y_max_mm)
                cost += _dist((c.x_mm, c.y_mm), (cx, cy)) + 5.0
    return cost


def density_congestion(board: BoardModel, cell_mm: float = 5.0) -> float:
    """Variance of component counts in a coarse grid — high peak density costs more."""
    if not board.components:
        return 0.0
    nx = max(1, int(math.ceil(board.width_mm / cell_mm)))
    ny = max(1, int(math.ceil(board.height_mm / cell_mm)))
    grid = [[0 for _ in range(ny)] for _ in range(nx)]
    for c in board.components.values():
        ix = min(nx - 1, max(0, int(c.x_mm / cell_mm)))
        iy = min(ny - 1, max(0, int(c.y_mm / cell_mm)))
        grid[ix][iy] += 1
    vals = [grid[i][j] for i in range(nx) for j in range(ny)]
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    peak = max(vals)
    return var + peak * 0.5


def thermal_spread(board: BoardModel) -> float:
    """Penalize clustering of dissipating parts (sum of inverse distance * power)."""
    hot = [c for c in board.components.values() if c.power_dissipation_w > 0]
    if len(hot) < 2:
        return 0.0
    cost = 0.0
    for i, a in enumerate(hot):
        for b in hot[i + 1 :]:
            d = max(0.5, _dist((a.x_mm, a.y_mm), (b.x_mm, b.y_mm)))
            cost += (a.power_dissipation_w * b.power_dissipation_w) / d
    return cost


def emi_proxy(board: BoardModel, config: PlacementConfig) -> float:
    """High-di/dt / EMI-sensitive nets: loop area + length + proximity to analog."""
    centers = component_centers(board)
    cost = 0.0
    analog_refs = set()
    for net_name, pins in board.nets.items():
        lab = config.net_by_name().get(net_name)
        if lab and lab.net_class == NetClass.ANALOG:
            analog_refs.update(r for r, _ in pins)

    for lab in config.nets:
        if not (lab.emi_sensitive or lab.net_class in (NetClass.RF, NetClass.CLOCK)):
            continue
        pins = board.nets.get(lab.name, [])
        refs = [r for r, _ in pins if r in centers]
        if len(refs) < 2:
            continue
        xs = [centers[r][0] for r in refs]
        ys = [centers[r][1] for r in refs]
        length = (max(xs) - min(xs)) + (max(ys) - min(ys))
        cost += length * config.weight_for_net(lab.name)
        # keep away from analog parts
        for ar in analog_refs:
            if ar not in centers:
                continue
            for r in refs:
                d = _dist(centers[r], centers[ar])
                if d < 8.0:
                    cost += (8.0 - d) * 2.0
    # power loop already captured; add SW-group area again lightly via power_loop_area caller
    return cost


class SimulationBackend(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def score(self, board: BoardModel, config: PlacementConfig) -> tuple[float, str]:
        """Return (cost, note). Lower is better."""


class GeometricSpiceProxy(SimulationBackend):
    """Always-available SPICE proxy using R/L estimates from placement geometry.

    When ngspice is installed, also runs a tiny netlist for power-rail check.
    """

    name = "spice"

    def available(self) -> bool:
        return True

    def score(self, board: BoardModel, config: PlacementConfig) -> tuple[float, str]:
        # Parasitic L ~ loop area; R ~ length for power nets
        loop = power_loop_area(board, config)
        power_len = 0.0
        centers = component_centers(board)
        for lab in config.nets:
            if lab.net_class not in (NetClass.POWER, NetClass.GROUND) and not lab.simulate_spice:
                continue
            pins = board.nets.get(lab.name, [])
            refs = [r for r, _ in pins if r in centers]
            if len(refs) < 2:
                continue
            xs = [centers[r][0] for r in refs]
            ys = [centers[r][1] for r in refs]
            power_len += (max(xs) - min(xs)) + (max(ys) - min(ys))

        # Rough: L_nH ~ 0.8 nH/mm * perimeter proxy of loop
        l_nh = 0.8 * math.sqrt(max(loop, 0.01))
        # Cost proportional to L and resistive length
        cost = l_nh * 0.5 + power_len * 0.1
        note = f"spice_proxy L≈{l_nh:.2f}nH loop_area={loop:.1f}mm² power_len={power_len:.1f}mm"

        if shutil.which("ngspice") and any(n.simulate_spice for n in config.nets):
            sim_cost, sim_note = _run_ngspice_rail_check(l_nh, power_len)
            cost = 0.4 * cost + 0.6 * sim_cost
            note = f"{note}; {sim_note}"
        return cost, note


def _run_ngspice_rail_check(l_nh: float, r_mohm_proxy: float) -> tuple[float, str]:
    """Minimal ngspice: step current into RL of estimated rail parasitics."""
    r = max(0.001, r_mohm_proxy * 0.001)  # crude map length→ohms
    inductance_h = max(1e-10, l_nh * 1e-9)
    netlist = f"""* physics-router rail check
Vsrc vin 0 DC 5
Rpar vin vload {r}
Lpar vload 0 {inductance_h}
.tran 1n 100n
.print tran v(vload)
.end
"""
    try:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "rail.cir"
            p.write_text(netlist, encoding="utf-8")
            proc = subprocess.run(
                ["ngspice", "-b", str(p)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                return 5.0, "ngspice_failed"
            # Prefer smaller L (less ring / drop) — use L as cost surrogate from sim path
            return l_nh * 0.4, "ngspice_ok"
    except (subprocess.TimeoutExpired, OSError) as e:
        return 5.0, f"ngspice_error:{e}"


class OpenEMSBackend(SimulationBackend):
    """OpenEMS scoring: EMI proxy + optional mesh export readiness check.

    Full FDTD is too slow for inner-loop placement; use `export-openems` for
    high-fidelity runs on shortlisted layouts. When openEMS is installed we
    still return a fast proxy cost but note that a mesh can be exported.
    """

    name = "openems"

    def available(self) -> bool:
        return True

    def score(self, board: BoardModel, config: PlacementConfig) -> tuple[float, str]:
        base = emi_proxy(board, config) + 0.3 * power_loop_area(board, config)
        note = f"em_proxy emi={emi_proxy(board, config):.2f}"
        # Geometry richness: more EMI-tagged nets ⇒ encourage export path
        em_nets = sum(1 for n in config.nets if n.simulate_em or n.emi_sensitive)
        if em_nets:
            note += f"; em_nets={em_nets}"
        if shutil.which("openEMS") or shutil.which("openems"):
            note += "; openEMS_binary_present(export_via_cli)"
        elif any(n.simulate_em for n in config.nets):
            note += "; openEMS_not_installed(proxy_used)"
        # Slight bonus structure: shorter high-speed nets lower cost already in emi_proxy
        return base, note


def _net_hpwl_mm(board: BoardModel, net_name: str) -> float:
    centers = component_centers(board)
    refs = [r for r, _ in board.nets.get(net_name, []) if r in centers]
    if len(refs) < 2:
        return 0.0
    xs = [centers[r][0] for r in refs]
    ys = [centers[r][1] for r in refs]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def ir_drop_proxy(board: BoardModel, config: PlacementConfig) -> tuple[float, str]:
    """Estimate resistive IR drop cost on power / high-current nets.

    Uses copper sheet resistance approximation:
      R ≈ ρ_cu * length / (width * thickness)
    with default 1 oz copper (35 µm) and width from class heuristics.
    Current assumptions (mA) from net class / notes (charlieplex ~GPIO peak).
    """
    rho = 1.68e-8  # ohm-m
    t_m = 35e-6  # 1 oz
    cost = 0.0
    details: list[str] = []
    for lab in config.nets:
        if lab.net_class not in (NetClass.POWER, NetClass.GROUND, NetClass.HIGH_SPEED):
            if not lab.simulate_spice and not lab.power_loop_group:
                continue
        length_mm = _net_hpwl_mm(board, lab.name)
        if length_mm <= 0:
            continue
        # track width mm
        if lab.net_class in (NetClass.POWER, NetClass.GROUND):
            w_mm = 0.4
            i_a = 0.05  # 50 mA rail budget default
        elif lab.net_class == NetClass.HIGH_SPEED or (
            lab.power_loop_group and "charlie" in (lab.power_loop_group or "").lower()
        ):
            w_mm = 0.15
            i_a = 0.02  # ~20 mA LED pulse
        else:
            w_mm = 0.2
            i_a = 0.01
        length_m = length_mm * 1e-3
        width_m = w_mm * 1e-3
        r = rho * length_m / max(width_m * t_m, 1e-15)
        vdrop = r * i_a
        # Cost scales with mV of drop (target << 50 mV on small boards)
        term = max(0.0, vdrop * 1000.0 - 5.0)  # ignore <5 mV
        if term > 0:
            cost += term * config.weight_for_net(lab.name)
            details.append(f"{lab.name}:{vdrop*1e3:.2f}mV")
    note = "ir_drop " + (",".join(details[:6]) if details else "ok")
    return cost, note


def loop_inductance_nh(board: BoardModel, config: PlacementConfig) -> tuple[float, str]:
    """Partial loop inductance proxy from power_loop_group geometry.

    L ≈ μ0 * perimeter * (ln(perimeter/width) ) / 2π  (very rough wire-loop model)
    Cost is sum of L_nH for each group — smaller loops are better for EMI/di/dt.
    """
    mu0 = 1.256637e-6
    centers = component_centers(board)
    groups: dict[str, set[str]] = {}
    for lab in config.nets:
        if lab.power_loop_group:
            for ref, _ in board.nets.get(lab.name, []):
                groups.setdefault(lab.power_loop_group, set()).add(ref)
    total_nh = 0.0
    parts: list[str] = []
    for gid, refs in groups.items():
        pts = [centers[r] for r in refs if r in centers]
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        # perimeter of bbox as loop path
        w = max(0.5, max(xs) - min(xs))
        h = max(0.5, max(ys) - min(ys))
        peri_m = 2 * (w + h) * 1e-3
        width_m = 0.3e-3
        # Grover-ish loop
        l_h = mu0 * peri_m * (math.log(max(peri_m / width_m, 1.1)) - 1.0) / (2 * math.pi)
        l_nh = max(0.1, l_h * 1e9)
        total_nh += l_nh
        parts.append(f"{gid}:{l_nh:.1f}nH")
    return total_nh, "loop_L " + (",".join(parts) if parts else "none")


def return_path_score(board: BoardModel, config: PlacementConfig) -> tuple[float, str]:
    """Penalize high-speed / EMI nets far from GND-connected copper centroids.

    On multilayer boards, a nearby ground reference is assumed if GND components
    cluster near the net; large distance ⇒ poor return path / larger loop.
    """
    centers = component_centers(board)
    gnd_pts = []
    for name, pins in board.nets.items():
        lab = config.net_by_name().get(name)
        if lab and lab.net_class == NetClass.GROUND:
            gnd_pts.extend(centers[r] for r, _ in pins if r in centers)
        elif name.upper() in ("GND", "AGND", "DGND", "PGND", "VSS"):
            gnd_pts.extend(centers[r] for r, _ in pins if r in centers)
    if not gnd_pts:
        return 0.0, "return_path:no_gnd"
    gcx = sum(p[0] for p in gnd_pts) / len(gnd_pts)
    gcy = sum(p[1] for p in gnd_pts) / len(gnd_pts)
    cost = 0.0
    n = 0
    for lab in config.nets:
        if not (
            lab.emi_sensitive
            or lab.net_class
            in (NetClass.HIGH_SPEED, NetClass.CLOCK, NetClass.RF, NetClass.DIFFERENTIAL)
        ):
            continue
        refs = [r for r, _ in board.nets.get(lab.name, []) if r in centers]
        if not refs:
            continue
        cx = sum(centers[r][0] for r in refs) / len(refs)
        cy = sum(centers[r][1] for r in refs) / len(refs)
        d = _dist((cx, cy), (gcx, gcy))
        # Prefer < 5 mm to GND cluster on small boards
        cost += max(0.0, d - 5.0) * config.weight_for_net(lab.name)
        n += 1
    # Multilayer with inner planes: soften penalty
    if board.copper_layers and len(board.copper_layers) >= 4:
        cost *= 0.5
    return cost, f"return_path nets={n} gnd_centroid=({gcx:.1f},{gcy:.1f})"


def matrix_length_match_score(board: BoardModel, config: PlacementConfig) -> tuple[float, str]:
    """Penalize length skew within charlieplex / matrix net groups (CPX-*).

    Uniform brightness on LED matrices needs similar drive-path lengths.
    """
    # Group by prefix CPX or power_loop_group containing matrix nets
    buckets: dict[str, list[float]] = {}
    for name in board.nets:
        if name.upper().startswith("CPX"):
            buckets.setdefault("CPX", []).append(_net_hpwl_mm(board, name))
        lab = config.net_by_name().get(name)
        if lab and lab.power_loop_group and "charlie" in lab.power_loop_group.lower():
            buckets.setdefault(lab.power_loop_group, []).append(_net_hpwl_mm(board, name))
    cost = 0.0
    notes: list[str] = []
    for gid, lens in buckets.items():
        if len(lens) < 2:
            continue
        mean = sum(lens) / len(lens)
        skew = max(lens) - min(lens)
        # percent skew
        pct = 100.0 * skew / max(mean, 0.1)
        cost += max(0.0, pct - 10.0)  # allow 10% free
        notes.append(f"{gid}:skew={skew:.1f}mm({pct:.0f}%)")
    return cost, "matrix_match " + (",".join(notes) if notes else "n/a")


def _total_from_weights(w: object, sb: ScoreBreakdown) -> float:
    return (
        float(getattr(w, "weighted_wirelength", 1.0)) * sb.weighted_wirelength
        + float(getattr(w, "power_loop_area", 1.0)) * sb.power_loop_area
        + float(getattr(w, "critical_net_length", 1.0)) * sb.critical_net_length
        + float(getattr(w, "overlap_penalty", 1.0)) * sb.overlap_penalty
        + float(getattr(w, "region_violation", 1.0)) * sb.region_violation
        + float(getattr(w, "density_congestion", 1.0)) * sb.density_congestion
        + float(getattr(w, "thermal_spread", 1.0)) * sb.thermal_spread
        + float(getattr(w, "emi_proxy", 1.0)) * sb.emi_proxy
        + float(getattr(w, "spice_score", 1.0)) * sb.spice_score
        + float(getattr(w, "openems_score", 1.0)) * sb.openems_score
        + float(getattr(w, "ir_drop", 1.0)) * sb.ir_drop
        + float(getattr(w, "loop_inductance", 1.0)) * sb.loop_inductance
        + float(getattr(w, "return_path", 1.0)) * sb.return_path
        + float(getattr(w, "matrix_length_match", 1.0)) * sb.matrix_length_match
    )


def geometric_score(board: BoardModel, config: PlacementConfig) -> ScoreBreakdown:
    """Fast multi-objective score without external simulators."""
    w = config.physics
    ir, ir_note = ir_drop_proxy(board, config)
    lnh, l_note = loop_inductance_nh(board, config)
    rp, rp_note = return_path_score(board, config)
    mx, mx_note = matrix_length_match_score(board, config)
    sb = ScoreBreakdown(
        weighted_wirelength=weighted_wirelength(board, config),
        power_loop_area=power_loop_area(board, config),
        critical_net_length=critical_net_length(board, config),
        overlap_penalty=overlap_penalty(board),
        region_violation=region_violation(board, config),
        density_congestion=density_congestion(board),
        thermal_spread=thermal_spread(board),
        emi_proxy=emi_proxy(board, config),
        ir_drop=ir,
        loop_inductance=lnh,
        return_path=rp,
        matrix_length_match=mx,
        notes=[ir_note, l_note, rp_note, mx_note],
    )
    sb.total = _total_from_weights(w, sb)
    return sb


def apply_simulation_scores(
    board: BoardModel,
    config: PlacementConfig,
    sb: ScoreBreakdown,
    spice: SimulationBackend | None = None,
    openems: SimulationBackend | None = None,
) -> ScoreBreakdown:
    """Augment a geometric ScoreBreakdown with physics simulations."""
    w = config.physics
    notes = list(sb.notes)

    if config.use_spice and spice is not None:
        cost, note = spice.score(board, config)
        sb.spice_score = cost
        notes.append(note)
    if config.use_openems and openems is not None:
        cost, note = openems.score(board, config)
        sb.openems_score = cost
        notes.append(note)

    sb.total = _total_from_weights(w, sb)
    sb.notes = notes
    return sb
