"""Domain models: labeled nets, placement state, and scoring results."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class NetClass(str, Enum):
    """Semantic net class used for weighted placement and physics."""

    POWER = "power"
    GROUND = "ground"
    SIGNAL = "signal"
    CLOCK = "clock"
    DIFFERENTIAL = "differential"
    ANALOG = "analog"
    RF = "rf"
    HIGH_SPEED = "high_speed"
    RESET = "reset"
    OTHER = "other"


class NetLabel(BaseModel):
    """Well-labeled net with placement weight and designer notes."""

    name: str
    net_class: NetClass = NetClass.SIGNAL
    weight: float = Field(default=1.0, ge=0.0, description="Higher = pull connected parts closer")
    critical: bool = False
    max_length_mm: float | None = Field(default=None, gt=0)
    target_impedance_ohm: float | None = Field(default=None, gt=0)
    pair_with: str | None = Field(default=None, description="Mate net for differential pairs")
    notes: str = ""
    # Physics tags
    power_loop_group: str | None = Field(
        default=None,
        description="Nets in the same switcher/power loop share a group id",
    )
    simulate_spice: bool = False
    simulate_em: bool = False
    emi_sensitive: bool = False

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("net name must be non-empty")
        return v


class FixedPlacement(BaseModel):
    """Component forced to a position (connectors, UI, mechanical)."""

    ref: str
    x_mm: float
    y_mm: float
    rotation_deg: float = 0.0
    locked: bool = True
    notes: str = ""


class RegionConstraint(BaseModel):
    """Named board region for floorplanning (e.g. analog, power)."""

    name: str
    x_min_mm: float
    y_min_mm: float
    x_max_mm: float
    y_max_mm: float
    preferred_refs: list[str] = Field(default_factory=list)
    preferred_net_classes: list[NetClass] = Field(default_factory=list)
    notes: str = ""


class PhysicsWeights(BaseModel):
    """Multi-objective weights for placement scoring (higher = more important)."""

    weighted_wirelength: float = 1.0
    power_loop_area: float = 3.0
    critical_net_length: float = 2.5
    overlap_penalty: float = 50.0
    region_violation: float = 10.0
    density_congestion: float = 1.5
    thermal_spread: float = 0.5
    emi_proxy: float = 2.0
    spice_score: float = 4.0
    openems_score: float = 4.0
    # High-impact physics extensions
    ir_drop: float = 3.0
    loop_inductance: float = 3.5
    return_path: float = 2.5
    matrix_length_match: float = 3.0


class PlacementConfig(BaseModel):
    """Project-level placement configuration (YAML/JSON next to KiCad files)."""

    project_name: str = "unnamed"
    board_width_mm: float = 100.0
    board_height_mm: float = 80.0
    grid_mm: float = 0.5
    nets: list[NetLabel] = Field(default_factory=list)
    fixed: list[FixedPlacement] = Field(default_factory=list)
    regions: list[RegionConstraint] = Field(default_factory=list)
    physics: PhysicsWeights = Field(default_factory=PhysicsWeights)
    # Search
    num_candidates: int = Field(default=8, ge=1, le=64)
    sa_iterations: int = Field(default=2000, ge=100)
    sa_initial_temp: float = 10.0
    sa_cooling: float = 0.995
    random_seed: int = 42
    # Simulation policy
    use_spice: bool = True
    use_openems: bool = True
    spice_on_top_n: int = Field(
        default=3,
        ge=0,
        description="Run Ngspice only on the top-N geometric candidates",
    )
    openems_on_top_n: int = Field(
        default=2,
        ge=0,
        description="Run OpenEMS only on the top-N after spice ranking",
    )
    notes: str = ""

    def net_by_name(self) -> dict[str, NetLabel]:
        return {n.name: n for n in self.nets}

    def weight_for_net(self, name: str) -> float:
        n = self.net_by_name().get(name)
        if n is None:
            return 1.0
        w = n.weight
        if n.critical:
            w *= 2.0
        if n.net_class in (NetClass.POWER, NetClass.GROUND, NetClass.CLOCK, NetClass.RF):
            w *= 1.5
        return w


class Component(BaseModel):
    """Placeable footprint instance."""

    ref: str
    footprint: str = ""
    width_mm: float = 2.0
    height_mm: float = 2.0
    x_mm: float = 0.0
    y_mm: float = 0.0
    rotation_deg: float = 0.0
    layer: str = "F.Cu"
    locked: bool = False
    power_dissipation_w: float = 0.0
    pads: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = ""

    @property
    def center(self) -> tuple[float, float]:
        return (self.x_mm, self.y_mm)


class BoardModel(BaseModel):
    """In-memory board: components, connectivity, outline, design rules."""

    width_mm: float
    height_mm: float
    components: dict[str, Component] = Field(default_factory=dict)
    # net_name -> list of (ref, pad)
    nets: dict[str, list[tuple[str, str]]] = Field(default_factory=dict)
    source_path: str | None = None
    # Populated from KiCad stackup / DRC when available (dict for JSON friendliness)
    design_rules: dict | None = None
    copper_layers: list[str] = Field(default_factory=lambda: ["F.Cu", "B.Cu"])

    def movable_refs(self) -> list[str]:
        return [r for r, c in self.components.items() if not c.locked]


class ScoreBreakdown(BaseModel):
    """Per-term costs (lower is better) and total."""

    weighted_wirelength: float = 0.0
    power_loop_area: float = 0.0
    critical_net_length: float = 0.0
    overlap_penalty: float = 0.0
    region_violation: float = 0.0
    density_congestion: float = 0.0
    thermal_spread: float = 0.0
    emi_proxy: float = 0.0
    spice_score: float = 0.0
    openems_score: float = 0.0
    ir_drop: float = 0.0
    loop_inductance: float = 0.0
    return_path: float = 0.0
    matrix_length_match: float = 0.0
    total: float = 0.0
    notes: list[str] = Field(default_factory=list)

    def as_dict(self) -> dict[str, float]:
        return {
            "weighted_wirelength": self.weighted_wirelength,
            "power_loop_area": self.power_loop_area,
            "critical_net_length": self.critical_net_length,
            "overlap_penalty": self.overlap_penalty,
            "region_violation": self.region_violation,
            "density_congestion": self.density_congestion,
            "thermal_spread": self.thermal_spread,
            "emi_proxy": self.emi_proxy,
            "spice_score": self.spice_score,
            "openems_score": self.openems_score,
            "ir_drop": self.ir_drop,
            "loop_inductance": self.loop_inductance,
            "return_path": self.return_path,
            "matrix_length_match": self.matrix_length_match,
            "total": self.total,
        }


class PlacementCandidate(BaseModel):
    """One scored placement proposal."""

    candidate_id: int
    positions: dict[str, tuple[float, float, float]]  # ref -> (x, y, rot)
    score: ScoreBreakdown
    rank: int | None = None
