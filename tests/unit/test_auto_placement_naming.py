"""An auto-placed fixture keeps the display name the placement reported.

Defect: the tree labelled an auto-placed device with its **device id**
(``dev_auto_office_office_pot_lights``) even though ``/discover/placements``
returned ``"name": "Office Pot Lights"``.  The id is a slug derived from the
entity id; it is not what the fixture is called, and the engine must not invent a
name from it.

The name cannot live on ``DeviceConfiguration``: a device's display name belongs
to the topology row (``DeviceTopology.name``).  So it travels on the placement
(it is not a persisted model) and is handed to materialisation, which writes it
to the row.

One test per link in that chain, plus the negative one: with no name supplied the
engine must fall back to the id rather than title-casing the slug.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceConfiguration,
    ExpectedPower,
    HaConnectivity,
    MonitoringLevel,
    Phase,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation.auto_placement import (
    PlacementPlan,
    auto_device_id,
    plan_placements,
)
from wattmosaic_engine.disaggregation.configuration import project_configuration
from wattmosaic_engine.electrical.panel_tree import build_panel_tree
from wattmosaic_engine.ingestion.correlation import CorrelationReport, CtCandidate

ENTITY = "light.office_office_pot_lights"
FRIENDLY = "Office Pot Lights"


def _topology() -> Topology:
    return Topology(
        status=TopologyStatus.CONFIRMED,
        version=1,
        channels=[
            CTChannel(
                ct_id="main",
                entity_id="sensor.main_power",
                ct_type=CTType.MAIN,
                phase=Phase.BOTH,
            ),
            CTChannel(
                ct_id="lights_ct1",
                entity_id="sensor.lights_ct1_power",
                name="Lights CT1",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
            ),
        ],
        devices=[],
    )


def _report() -> CorrelationReport:
    """The candidate the correlation pass produces for one switch on one row."""
    return CorrelationReport(
        candidates=(
            CtCandidate(
                entity_id=ENTITY,
                ct_id="lights_ct1",
                corroborations=3,
                agreement=0.9,
                typical_on_w=19.57,
                typical_off_w=19.42,
                evidence=("row moved on every transition",),
            ),
        )
    )


def _plan() -> PlacementPlan:
    return plan_placements(
        report=_report(),
        topology=_topology(),
        configurations=[],
        labels={ENTITY: FRIENDLY},
    )


def _walk(nodes: Iterable[dict[str, Any]]):
    for node in nodes:
        yield node
        yield from _walk(node.get("children") or [])


def test_the_plan_carries_the_friendly_name_it_was_given() -> None:
    plan = _plan()
    assert [placement.name for placement in plan.accepted] == [FRIENDLY]
    # It is display data only: the placement decision is unchanged by the label.
    assert plan.device_ids() == (auto_device_id(ENTITY),)


def test_a_materialised_auto_device_is_labelled_with_the_reported_name() -> None:
    """End to end: plan -> project -> tree row -> rendered label.

    The expected name is taken from the plan, never hand-typed, so this cannot
    pass by coincidentally matching a literal that the code under test invented.
    """
    plan = _plan()
    expected = plan.accepted[0].name
    assert expected == FRIENDLY  # the fixture's name is what the plan reported

    projected = project_configuration(
        _topology(),
        [placement.configuration for placement in plan.accepted],
        display_names={placement.device_id: placement.name for placement in plan.accepted},
    )
    device = projected.device_map()[auto_device_id(ENTITY)]

    assert device.name == expected
    assert device.name != device.device_id

    tree = build_panel_tree(projected)
    node = next(
        item
        for item in _walk(tree["roots"])
        if item["kind"] == "device" and item["device_id"] == auto_device_id(ENTITY)
    )
    assert node["label"] == expected


def test_without_a_name_the_engine_keeps_the_id_and_never_invents_one() -> None:
    """Negative test: no label means no label.

    Reverting the fix makes this fail in the other direction -- a title-cased
    slug (``Office Office Pot Lights``) would appear where no name was ever
    reported.
    """
    plan = plan_placements(report=_report(), topology=_topology(), configurations=[])
    assert all(placement.name is None for placement in plan.accepted)

    projected = project_configuration(
        _topology(), [placement.configuration for placement in plan.accepted]
    )
    device = projected.device_map()[auto_device_id(ENTITY)]

    assert device.name == device.device_id
    assert " " not in str(device.name)


def test_the_configuration_itself_never_carries_the_display_name() -> None:
    """The persisted contract stays untouched: the name is not a config field."""
    configuration = DeviceConfiguration(
        device_id=auto_device_id(ENTITY),
        connectivity=HaConnectivity.CONNECTED,
        monitoring=MonitoringLevel.SHARED,
        state_entity_ids=[ENTITY],
        monitored_by_ct_ids=["lights_ct1"],
        configured_by="auto",
        expected=ExpectedPower(on_w=19.57),
    )
    assert "name" not in configuration.model_dump()


def test_a_second_apply_repairs_a_device_stored_with_its_id_as_the_name() -> None:
    """An install that already ran the buggy code is healed by re-applying.

    Before the fix, materialisation stored ``name = device_id``.  That row now
    exists in ``topology.devices``, so it takes the "existing row" path rather
    than materialisation -- which is exactly why ``project_configuration`` also
    applies the supplied name to existing rows.
    """
    from wattmosaic_engine.contracts import DeviceTopology

    plan = _plan()
    placement = plan.accepted[0]
    already_broken = _topology().model_copy(
        update={
            "devices": [
                DeviceTopology(
                    device_id=placement.device_id,
                    name=placement.device_id,  # what the old code wrote
                    branch_ct_ids=["lights_ct1"],
                )
            ]
        }
    )

    projected = project_configuration(
        already_broken,
        [placement.configuration],
        display_names={placement.device_id: placement.name},
    )

    assert projected.device_map()[placement.device_id].name == FRIENDLY


def test_apply_writes_the_friendly_name_the_state_snapshot_reported(tmp_path) -> None:
    """The real ``discover_placements`` path, end to end.

    The step this pins is the one that was missing: the state snapshot's
    ``friendly_name`` must survive ``plan_placements`` -> ``project_configuration``
    and land on the persisted topology row.  Reverting any link in that chain
    (dropping the label, or not passing the placement's name through) makes the
    device fall back to its id and this fails.

    The recorder/history fixture is the one the placement tests already use; it is
    imported rather than copied so this test cannot drift into proving a
    hand-built plan that production never constructs.
    """
    from tests.unit.test_discover_placements import _core

    core, db, _ = _core(
        tmp_path,
        auto_placement=True,
        states=[
            {
                "entity_id": "light.first",
                "state": "off",
                "attributes": {"friendly_name": "Kitchen Pendant"},
            },
            {
                "entity_id": "light.second",
                "state": "off",
                "attributes": {"friendly_name": "Hallway Sconce"},
            },
        ],
    )

    asyncio.run(core.discover_placements(lookback_hours=24, apply=True))

    projected = db.load_topology()
    assert projected is not None
    names = {
        device.device_id: device.name
        for device in projected.devices
        if device.device_id.startswith("dev_auto_")
    }
    assert names == {
        auto_device_id("light.first"): "Kitchen Pendant",
        auto_device_id("light.second"): "Hallway Sconce",
    }
    assert all(name != device_id for device_id, name in names.items())
