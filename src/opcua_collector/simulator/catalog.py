"""Address space of the bundled simulator.

Node ids are stable on purpose. `config.example.yaml` and the Docker config
copy them, and a test fails if the two drift apart. The namespace index is
2 because a stock OPC UA server already uses indexes 0 and 1.
"""

from __future__ import annotations

from dataclasses import dataclass

NAMESPACE_URI = "urn:opcua-collector:simulator"
SIMULATOR_NAMESPACE_INDEX = 2


@dataclass(frozen=True)
class CatalogNode:
    identifier: str
    browse_name: str
    value_type: str
    column: str | None
    unit: str | None
    description: str
    # Collected nodes are the end-of-cycle snapshot. The others move while
    # a shot is in progress and are there for anyone browsing the server.
    collected: bool


CATALOG: tuple[CatalogNode, ...] = (
    CatalogNode(
        identifier="Machine.CycleCounter",
        browse_name="CycleCounter",
        value_type="integer",
        column="cycle_number",
        unit=None,
        description="Completed-shot counter. The collector subscribes to this node.",
        collected=False,
    ),
    CatalogNode(
        identifier="Machine.MeltTemperature",
        browse_name="MeltTemperature",
        value_type="float",
        column="melt_temperature",
        unit="°C",
        description="Melt temperature at the end of the shot",
        collected=True,
    ),
    CatalogNode(
        identifier="Machine.MoldTemperature",
        browse_name="MoldTemperature",
        value_type="float",
        column="mold_temperature",
        unit="°C",
        description="Mold temperature at the end of the shot",
        collected=True,
    ),
    CatalogNode(
        identifier="Machine.InjectionPressure",
        browse_name="InjectionPressure",
        value_type="float",
        column="injection_pressure",
        unit="MPa",
        description="Peak injection pressure during the shot",
        collected=True,
    ),
    CatalogNode(
        identifier="Machine.HoldPressure",
        browse_name="HoldPressure",
        value_type="float",
        column="hold_pressure",
        unit="MPa",
        description="Hold pressure applied after injection",
        collected=True,
    ),
    CatalogNode(
        identifier="Machine.InjectionTime",
        browse_name="InjectionTime",
        value_type="float",
        column="injection_time",
        unit="s",
        description="Duration of the injection phase",
        collected=True,
    ),
    CatalogNode(
        identifier="Machine.CoolingTime",
        browse_name="CoolingTime",
        value_type="float",
        column="cooling_time",
        unit="s",
        description="Duration of the cooling phase",
        collected=True,
    ),
    CatalogNode(
        identifier="Machine.CycleTime",
        browse_name="CycleTime",
        value_type="float",
        column="cycle_time",
        unit="s",
        description="Shot-to-shot time reported by the machine",
        collected=True,
    ),
    CatalogNode(
        identifier="Machine.ShotVolume",
        browse_name="ShotVolume",
        value_type="float",
        column="shot_volume",
        unit="cm³",
        description="Volume injected during the shot",
        collected=True,
    ),
    CatalogNode(
        identifier="Machine.ClampForce",
        browse_name="ClampForce",
        value_type="float",
        column="clamp_force",
        unit="kN",
        description="Clamp force during the shot",
        collected=True,
    ),
    CatalogNode(
        identifier="Machine.ScrewCushion",
        browse_name="ScrewCushion",
        value_type="float",
        column="screw_cushion",
        unit="mm",
        description="Screw cushion after injection",
        collected=True,
    ),
    CatalogNode(
        identifier="Machine.PartQuality",
        browse_name="PartQuality",
        value_type="string",
        column="part_quality",
        unit=None,
        description="Shot judgement: good, review, scrap, or idle before the first shot",
        collected=True,
    ),
    CatalogNode(
        identifier="Machine.AlarmActive",
        browse_name="AlarmActive",
        value_type="boolean",
        column="alarm_active",
        unit=None,
        description="True when the shot was judged scrap",
        collected=True,
    ),
    CatalogNode(
        identifier="Machine.State",
        browse_name="State",
        value_type="string",
        column=None,
        unit=None,
        description="Current machine phase. Moves during the cycle; not part of the snapshot.",
        collected=False,
    ),
    CatalogNode(
        identifier="Machine.ScrewPosition",
        browse_name="ScrewPosition",
        value_type="float",
        column=None,
        unit="mm",
        description="Live screw position, 0 mm fully forward",
        collected=False,
    ),
    CatalogNode(
        identifier="Machine.HydraulicPressure",
        browse_name="HydraulicPressure",
        value_type="float",
        column=None,
        unit="MPa",
        description="Live hydraulic pressure",
        collected=False,
    ),
)


def trigger_node() -> CatalogNode:
    return CATALOG[0]


def collected_nodes() -> tuple[CatalogNode, ...]:
    return tuple(node for node in CATALOG if node.collected)


def node_id(identifier: str, namespace_index: int = SIMULATOR_NAMESPACE_INDEX) -> str:
    return f"ns={namespace_index};s={identifier}"
