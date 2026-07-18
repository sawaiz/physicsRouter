"""Conflict-cluster repair using bounded CBS-style branching.

1. Detect geometric conflicts between nets (clearance near-misses)
2. Build conflict graph → connected components
3. Small components: CBS-like re-route with forbidden regions
4. Optional CP-SAT for tiny via-assignment clusters (ortools if present)
"""

from __future__ import annotations

import copy
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from physics_router.models import BoardModel, PlacementConfig
from physics_router.router import (
    ObstacleMap,
    RouteResult,
    RouteSegment,
    Via,
    audit_same_layer_clearance,
    build_obstacle_map,
    free_angle_route,
    _dist,
    _point_seg_dist,
)
from physics_router.homotopy import k_homotopy_paths


@dataclass
class Conflict:
    net_a: str
    net_b: str
    layer: str
    x: float
    y: float
    distance_mm: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "nets": [self.net_a, self.net_b],
            "layer": self.layer,
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "distance_mm": round(self.distance_mm, 3),
        }


@dataclass
class ConflictCluster:
    nets: list[str]
    conflicts: list[Conflict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"nets": self.nets, "n_conflicts": len(self.conflicts)}


def detect_conflicts(
    result: RouteResult,
    *,
    clearance_mm: float = 0.2,
) -> list[Conflict]:
    """Find foreign-net near-misses on the same layer."""
    by_layer: dict[str, list[RouteSegment]] = defaultdict(list)
    for s in result.segments:
        by_layer[s.layer].append(s)
    out: list[Conflict] = []
    seen: set[tuple[str, str, str]] = set()
    for ly, segs in by_layer.items():
        for i, a in enumerate(segs):
            for b in segs[i + 1 :]:
                if a.net == b.net:
                    continue
                need = clearance_mm + 0.5 * (a.width_mm + b.width_mm)
                # sample a few points
                for t in (0.0, 0.5, 1.0):
                    px = a.x1 + (a.x2 - a.x1) * t
                    py = a.y1 + (a.y2 - a.y1) * t
                    d = _point_seg_dist(px, py, b.x1, b.y1, b.x2, b.y2)
                    if d < need:
                        key = tuple(sorted([a.net, b.net]) + [ly])  # type: ignore[operator]
                        key = (key[0], key[1], ly)
                        if key in seen:
                            break
                        seen.add(key)
                        out.append(
                            Conflict(
                                net_a=a.net,
                                net_b=b.net,
                                layer=ly,
                                x=px,
                                y=py,
                                distance_mm=d,
                            )
                        )
                        break
    return out


def conflict_clusters(conflicts: list[Conflict]) -> list[ConflictCluster]:
    """Connected components of the net conflict graph."""
    adj: dict[str, set[str]] = defaultdict(set)
    conf_by_edge: dict[tuple[str, str], list[Conflict]] = defaultdict(list)
    for c in conflicts:
        adj[c.net_a].add(c.net_b)
        adj[c.net_b].add(c.net_a)
        e = (min(c.net_a, c.net_b), max(c.net_a, c.net_b))
        conf_by_edge[e].append(c)

    seen: set[str] = set()
    clusters: list[ConflictCluster] = []
    for n in adj:
        if n in seen:
            continue
        q = deque([n])
        comp: list[str] = []
        while q:
            u = q.popleft()
            if u in seen:
                continue
            seen.add(u)
            comp.append(u)
            for v in adj[u]:
                if v not in seen:
                    q.append(v)
        cl_confs: list[Conflict] = []
        for i, a in enumerate(comp):
            for b in comp[i + 1 :]:
                e = (min(a, b), max(a, b))
                cl_confs.extend(conf_by_edge.get(e, []))
        clusters.append(ConflictCluster(nets=sorted(comp), conflicts=cl_confs))
    clusters.sort(key=lambda c: len(c.nets))
    return clusters


def _strip_net(result: RouteResult, net: str) -> RouteResult:
    segs = [s for s in result.segments if s.net != net]
    vias = [v for v in result.vias if v.net != net]
    unrouted = [u for u in result.unrouted_nets if u != net]
    total = sum(_dist((s.x1, s.y1), (s.x2, s.y2)) for s in segs)
    reports = [r for r in result.net_reports if r.net != net]
    return RouteResult(
        segments=segs,
        vias=vias,
        via_count=len(vias),
        total_length_mm=total,
        unrouted_nets=unrouted,
        clearance_violations=result.clearance_violations,
        notes=list(result.notes),
        net_reports=reports,
        quality=dict(result.quality or {}),
    )


def _anchors_for_net(board: BoardModel, net: str) -> list[tuple[float, float]]:
    from physics_router.router import fanout_anchor

    pins = board.nets.get(net) or []
    anchors: list[tuple[float, float]] = []
    for ref, _pad in pins:
        if ref in board.components:
            anchors.append(fanout_anchor(board, ref, net))
    uniq: list[tuple[float, float]] = []
    for a in anchors:
        if not any(_dist(a, u) < 0.05 for u in uniq):
            uniq.append(a)
    return uniq


def _rebuild_om(
    board: BoardModel,
    result: RouteResult,
    *,
    clearance_mm: float,
    layers: list[str],
    forbid_regions: list[tuple[float, float, float, str]] | None = None,
) -> ObstacleMap:
    om = build_obstacle_map(board, clearance_mm=clearance_mm, layers=layers)
    for s in result.segments:
        om.paint_trace(s.x1, s.y1, s.x2, s.y2, s.layer, s.width_mm, s.net)
    for v in result.vias:
        for ly in layers:
            om.add_rect(v.x, v.y, v.size_mm, v.size_mm, ly, net=v.net, inflate=True)
    # Forbidden disks for CBS constraints
    for x, y, r, ly in forbid_regions or []:
        om.add_rect(x, y, r * 2, r * 2, ly, net=None, inflate=False)
    return om


def cbs_repair_cluster(
    result: RouteResult,
    board: BoardModel,
    cluster: ConflictCluster,
    config: PlacementConfig | None = None,
    *,
    clearance_mm: float = 0.2,
    grid_mm: float = 0.5,
    max_branches: int = 8,
    k_homotopy: int = 3,
) -> tuple[RouteResult, dict[str, Any]]:
    """Bounded CBS: for each conflict, branch on which net is excluded from region."""
    layers = list(board.copper_layers) or ["F.Cu", "B.Cu"]
    log: dict[str, Any] = {
        "cluster_nets": cluster.nets,
        "branches": [],
        "method": "cbs",
    }
    if len(cluster.nets) > 12:
        log["skipped"] = "cluster too large for CBS"
        return result, log

    best = result
    best_conf = len(detect_conflicts(result, clearance_mm=clearance_mm))
    # Priority queue of (n_conflicts, branch_id, RouteResult, forbids)
    # Simple iterative: for each conflict, try re-routing each net with region forbid
    work = [(best_conf, result, [])]  # conf, route, forbids

    branch_id = 0
    while work and branch_id < max_branches:
        work.sort(key=lambda t: t[0])
        n_conf, cur, forbids = work.pop(0)
        if n_conf < best_conf:
            best_conf = n_conf
            best = cur
        if n_conf == 0:
            break
        confs = detect_conflicts(cur, clearance_mm=clearance_mm)
        confs = [c for c in confs if c.net_a in cluster.nets and c.net_b in cluster.nets]
        if not confs:
            break
        c0 = confs[0]
        for victim in (c0.net_a, c0.net_b):
            branch_id += 1
            if branch_id > max_branches:
                break
            new_forbid = list(forbids) + [(c0.x, c0.y, max(clearance_mm * 3, 0.6), c0.layer)]
            stripped = _strip_net(cur, victim)
            om = _rebuild_om(
                board, stripped, clearance_mm=clearance_mm, layers=layers,
                forbid_regions=new_forbid,
            )
            anchors = _anchors_for_net(board, victim)
            if len(anchors) < 2:
                continue
            # MST edges with K-homotopy pick
            from physics_router.router import RouteSegment as RS

            new_segs: list[RouteSegment] = list(stripped.segments)
            new_vias: list[Via] = list(stripped.vias)
            ok = True
            remaining = set(range(1, len(anchors)))
            tree = {0}
            while remaining:
                best_e = None
                for i in tree:
                    for j in remaining:
                        d = _dist(anchors[i], anchors[j])
                        if best_e is None or d < best_e[0]:
                            best_e = (d, i, j)
                assert best_e is not None
                _, ia, ib = best_e
                remaining.remove(ib)
                tree.add(ib)
                # Try layers
                placed = False
                for ly in layers:
                    cands = k_homotopy_paths(
                        anchors[ia], anchors[ib], ly, victim, om,
                        k=k_homotopy, grid_mm=grid_mm, width_mm=0.25,
                    )
                    if not cands:
                        continue
                    path = cands[0].points
                    for i in range(len(path) - 1):
                        seg = RS(
                            path[i][0], path[i][1], path[i + 1][0], path[i + 1][1],
                            layer=ly, net=victim, width_mm=0.25,
                        )
                        new_segs.append(seg)
                        om.paint_trace(
                            seg.x1, seg.y1, seg.x2, seg.y2, ly, 0.25, victim
                        )
                    placed = True
                    break
                if not placed:
                    ok = False
                    break
            if not ok:
                log["branches"].append({"victim": victim, "status": "fail"})
                continue
            total = sum(_dist((s.x1, s.y1), (s.x2, s.y2)) for s in new_segs)
            trial = RouteResult(
                segments=new_segs,
                vias=new_vias,
                via_count=len(new_vias),
                total_length_mm=total,
                unrouted_nets=list(stripped.unrouted_nets),
                clearance_violations=0,
                notes=list(cur.notes),
                net_reports=list(stripped.net_reports),
                quality=dict(cur.quality or {}),
            )
            n2 = len(detect_conflicts(trial, clearance_mm=clearance_mm))
            log["branches"].append({
                "victim": victim, "status": "ok", "conflicts_after": n2,
                "forbid": {"x": c0.x, "y": c0.y, "layer": c0.layer},
            })
            work.append((n2, trial, new_forbid))
            if n2 < best_conf:
                best_conf = n2
                best = trial

    best.notes = list(best.notes) + [
        f"cbs: cluster {cluster.nets} · best_conflicts={best_conf} · branches={branch_id}"
    ]
    log["best_conflicts"] = best_conf
    best.compute_quality()
    return best, log


def try_cpsat_via_assignment(
    vias: list[Via],
    board: BoardModel,
    *,
    layers: list[str] | None = None,
) -> dict[str, Any]:
    """Optional OR-Tools CP-SAT for discrete via layer-pair assignment.

    Falls back to a greedy note when ortools is unavailable.
    """
    layers = layers or list(board.copper_layers) or ["F.Cu", "B.Cu"]
    report: dict[str, Any] = {"n_vias": len(vias), "solver": None}
    if len(vias) == 0:
        report["status"] = "empty"
        return report
    if len(vias) > 40:
        report["status"] = "too_many"
        return report
    try:
        from ortools.sat.python import cp_model  # type: ignore
    except Exception:
        report["status"] = "ortools_unavailable"
        report["solver"] = "greedy_fallback"
        # Greedy: keep existing layer pairs
        report["assignments"] = [
            {"net": v.net, "x": v.x, "y": v.y, "layers": list(v.layers)} for v in vias
        ]
        return report

    model = cp_model.CpModel()
    # Variables: which outer pair for each via (0 = F-B, 1 = F-In1, …) simplified to index
    pairs = [(layers[0], layers[-1])]
    if len(layers) > 2:
        pairs.append((layers[0], layers[1]))
        pairs.append((layers[1], layers[-1]))
    pair_vars = []
    for i, v in enumerate(vias):
        pv = model.NewIntVar(0, len(pairs) - 1, f"via_{i}")
        pair_vars.append(pv)
    # Soft: prefer pair 0 (through)
    model.Minimize(sum(pair_vars))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 2.0
    status = solver.Solve(model)
    report["solver"] = "cp_sat"
    report["status"] = int(status)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        report["assignments"] = []
        for i, v in enumerate(vias):
            pi = solver.Value(pair_vars[i])
            report["assignments"].append({
                "net": v.net,
                "x": v.x,
                "y": v.y,
                "layers": list(pairs[pi]),
            })
            v.layers = pairs[pi]
    return report


def repair_route_conflicts(
    result: RouteResult,
    board: BoardModel,
    config: PlacementConfig | None = None,
    *,
    clearance_mm: float = 0.2,
    grid_mm: float = 0.5,
    max_cluster_size: int = 8,
    max_clusters: int = 4,
) -> tuple[RouteResult, dict[str, Any]]:
    """Full conflict-cluster repair pass."""
    conflicts = detect_conflicts(result, clearance_mm=clearance_mm)
    clusters = conflict_clusters(conflicts)
    report: dict[str, Any] = {
        "initial_conflicts": len(conflicts),
        "clusters": [c.to_dict() for c in clusters],
        "repairs": [],
    }
    cur = result
    for cl in clusters[:max_clusters]:
        if len(cl.nets) > max_cluster_size:
            report["repairs"].append({"nets": cl.nets, "skipped": "too_large"})
            continue
        if len(cl.nets) < 2:
            continue
        cur, log = cbs_repair_cluster(
            cur, board, cl, config,
            clearance_mm=clearance_mm, grid_mm=grid_mm,
        )
        report["repairs"].append(log)

    # CP-SAT via polish (optional)
    if cur.vias:
        report["cpsat_vias"] = try_cpsat_via_assignment(cur.vias, board)

    final_c = detect_conflicts(cur, clearance_mm=clearance_mm)
    report["final_conflicts"] = len(final_c)
    audit = audit_same_layer_clearance(cur, clearance_mm=clearance_mm)
    cur.clearance_violations = int(audit.get("near_miss_pairs", 0))
    cur.notes.append(
        f"conflict_repair: {report['initial_conflicts']}→{report['final_conflicts']} conflicts"
    )
    cur.quality = {**(cur.quality or {}), "conflict_repair": report}
    cur.compute_quality()
    return cur, report
