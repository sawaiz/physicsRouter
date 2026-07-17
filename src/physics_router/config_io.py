"""Load/save placement config (YAML/JSON) with labeled nets and notes."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from physics_router.models import PlacementConfig


def load_config(path: str | Path) -> PlacementConfig:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text) or {}
    elif path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        # Try YAML first, then JSON
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping: {path}")
    return PlacementConfig.model_validate(data)


def save_config(config: PlacementConfig, path: str | Path) -> None:
    path = Path(path)
    data = config.model_dump(mode="json")
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    else:
        path.write_text(
            yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )


def example_config() -> PlacementConfig:
    """Small demo config with labeled nets, weights, and notes."""
    from physics_router.models import (
        FixedPlacement,
        NetClass,
        NetLabel,
        PhysicsWeights,
        RegionConstraint,
    )

    return PlacementConfig(
        project_name="demo_buck",
        board_width_mm=50.0,
        board_height_mm=40.0,
        nets=[
            NetLabel(
                name="+5V",
                net_class=NetClass.POWER,
                weight=4.0,
                critical=True,
                power_loop_group="buck1",
                simulate_spice=True,
                notes="Main 5V rail after buck; keep short to load and bulk cap.",
            ),
            NetLabel(
                name="GND",
                net_class=NetClass.GROUND,
                weight=4.0,
                critical=True,
                power_loop_group="buck1",
                notes="Return for buck loop; minimize loop area with SW node.",
            ),
            NetLabel(
                name="SW",
                net_class=NetClass.POWER,
                weight=5.0,
                critical=True,
                power_loop_group="buck1",
                emi_sensitive=True,
                simulate_em=True,
                notes="Switcher node: place inductor and diode/FET tight.",
            ),
            NetLabel(
                name="CLK_MCU",
                net_class=NetClass.CLOCK,
                weight=3.5,
                critical=True,
                max_length_mm=15.0,
                notes="MCU crystal net; place X1 and load caps adjacent.",
            ),
            NetLabel(
                name="USB_DP",
                net_class=NetClass.DIFFERENTIAL,
                weight=3.0,
                critical=True,
                pair_with="USB_DM",
                target_impedance_ohm=90.0,
                simulate_em=True,
                notes="USB 2.0 D+; length-match with USB_DM.",
            ),
            NetLabel(
                name="USB_DM",
                net_class=NetClass.DIFFERENTIAL,
                weight=3.0,
                critical=True,
                pair_with="USB_DP",
                target_impedance_ohm=90.0,
                notes="USB 2.0 D-.",
            ),
            NetLabel(
                name="AIN0",
                net_class=NetClass.ANALOG,
                weight=2.5,
                critical=True,
                notes="Quiet analog input; keep away from SW node.",
            ),
        ],
        fixed=[
            FixedPlacement(
                ref="J1",
                x_mm=2.0,
                y_mm=20.0,
                rotation_deg=0.0,
                notes="USB connector fixed on board edge.",
            ),
        ],
        regions=[
            RegionConstraint(
                name="power",
                x_min_mm=0.0,
                y_min_mm=0.0,
                x_max_mm=25.0,
                y_max_mm=40.0,
                preferred_refs=["U1", "L1", "C_IN", "C_OUT", "D1"],
                preferred_net_classes=[NetClass.POWER, NetClass.GROUND],
                notes="Buck converter and power passives.",
            ),
            RegionConstraint(
                name="analog",
                x_min_mm=30.0,
                y_min_mm=0.0,
                x_max_mm=50.0,
                y_max_mm=20.0,
                preferred_net_classes=[NetClass.ANALOG],
                notes="Keep analog sensors away from switcher.",
            ),
        ],
        physics=PhysicsWeights(
            weighted_wirelength=1.0,
            power_loop_area=4.0,
            critical_net_length=3.0,
            overlap_penalty=50.0,
            spice_score=5.0,
            openems_score=5.0,
        ),
        num_candidates=6,
        sa_iterations=1500,
        use_spice=True,
        use_openems=True,
        notes="Demo: labeled nets with weights drive physics-aware placement.",
    )
