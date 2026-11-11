"""Learning intake: a declared device link must reach the sample collector.

**The defect these tests exist for**

A live install reported ``active_learning = off / blocked / ambiguous_topology``
with ``samples_seen: 0``, ``samples_accepted: 0``, ``samples_rejected: 0`` and
``dominant_rejection_count: 0`` across 33 samples over 11 minutes.  Under a
``candidate`` topology every *offered* sample is rejected as
``ambiguous_topology``, so a healthy pipeline shows ``rejected > 0``.  Zero seen
**and** zero rejected means the offer stage is empty: no sample ever reached the
filter.

The chain that produced that:

1. ``main.on_event`` links an entity to a device through the topology, and needs
   the entity to be a *runtime state* entity (``state_entity_for_device``).
2. Those ids come from ``DeviceTopology.metadata["state_entity_ids"]``.
3. The only ``device_links`` entry on the install was an **HA device-registry
   id** (``9a2b4caee1c94761ae1c0403873d39b3``), not an entity id.  Nothing
   resolved it, so the topology carried no state entity at all.
4. ``entity_links`` therefore linked only the hot tub's two CT *power* entities;
   those take the ``power_events_not_state`` branch and return before the
   transition buffer.  Thirteen real state changes were discarded at that
   bridge.

The integration must resolve a device-registry link to the device's *state*
entities (never its power/energy entities) and hand the engine explicit ids.
The engine stays host-agnostic: it never consults the HA registry.

Nothing here talks to Home Assistant.  The registry is a plain mapping and the
"HA source" is a stub that only calls ``EngineCore.on_event`` / ``on_sample``.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
PACKAGE_NAME = "wm_learning_intake_under_test"


def _load_package() -> types.ModuleType:
    """Package handle without executing ``__init__`` (it imports HA)."""
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(PACKAGE_DIR)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = module
    return module


_load_package()

state_links = importlib.import_module(f"{PACKAGE_NAME}.state_links")

#: A real HA device-registry id (32 hex, no dot, no domain).
HA_DEVICE_ID = "9a2b4caee1c94761ae1c0403873d39b3"
STATE_ENTITY = "binary_sensor.hot_tub_running"
STATUS_ENTITY = "sensor.hot_tub_status"
POWER_ENTITY = "sensor.hot_tub_power"
ENERGY_ENTITY = "sensor.hot_tub_energy"


# --------------------------------------------------------------------------- #
# Resolution: a device id becomes explicit state entities
# --------------------------------------------------------------------------- #


def _registry() -> list[dict[str, Any]]:
    """Registry rows for the live hot tub: state + status + two measurements."""
    return [
        {
            "entity_id": STATE_ENTITY,
            "device_id": HA_DEVICE_ID,
            "device_class": "running",
            "domain": "binary_sensor",
        },
        {
            "entity_id": STATUS_ENTITY,
            "device_id": HA_DEVICE_ID,
            "domain": "sensor",
        },
        {
            "entity_id": POWER_ENTITY,
            "device_id": HA_DEVICE_ID,
            "device_class": "power",
            "unit": "W",
            "domain": "sensor",
        },
        {
            "entity_id": ENERGY_ENTITY,
            "device_id": HA_DEVICE_ID,
            "device_class": "energy",
            "unit": "kWh",
            "domain": "sensor",
        },
    ]


def _resolve(
    links: dict[str, str],
    *,
    entities: list[dict[str, Any]] | None = None,
    engine_devices: list[str] | None = None,
    ct_entity_ids: dict[str, list[str]] | None = None,
):
    return state_links.resolve_links(
        links,
        entities=entities if entities is not None else _registry(),
        engine_devices=engine_devices if engine_devices is not None else ["dev_hot_tub"],
        ct_entity_ids=ct_entity_ids if ct_entity_ids is not None else {},
    )


class TestDeviceRegistryResolution:
    def test_a_device_id_resolves_to_its_state_entities(self) -> None:
        resolution = _resolve({"dev_hot_tub": HA_DEVICE_ID})

        assert resolution.state_entity_ids("dev_hot_tub") == (
            STATE_ENTITY,
            STATUS_ENTITY,
        )
        assert resolution.devices_without_state_entity == ()

    def test_power_and_energy_entities_are_never_state_sources(self) -> None:
        """A measurement cannot be a transition: it would be fabricated load."""
        resolution = _resolve({"dev_hot_tub": HA_DEVICE_ID})
        selected = set(resolution.state_entity_ids("dev_hot_tub"))

        assert POWER_ENTITY not in selected
        assert ENERGY_ENTITY not in selected

    def test_the_devices_own_ct_entity_is_excluded_without_attributes(self) -> None:
        """Even a bare sensor that *is* the CT must not become state.

        The registry does not carry ``unit_of_measurement``; the topology does.
        The CT entity id is therefore passed in explicitly and excluded on that
        evidence alone.  Otherwise a registry row with no attributes would turn
        a watt reading into a fake ``on``.
        """
        entities = [
            {"entity_id": "sensor.no_attributes_at_all", "device_id": HA_DEVICE_ID},
            {"entity_id": STATE_ENTITY, "device_id": HA_DEVICE_ID},
        ]
        resolution = _resolve(
            {"dev_hot_tub": HA_DEVICE_ID},
            entities=entities,
            ct_entity_ids={"dev_hot_tub": ["sensor.no_attributes_at_all"]},
        )
        assert resolution.state_entity_ids("dev_hot_tub") == (STATE_ENTITY,)

    def test_a_device_with_no_state_entities_is_reported_not_silent(self) -> None:
        """The silent discard is the core sin: name the device and the reason."""
        entities = [
            {
                "entity_id": POWER_ENTITY,
                "device_id": HA_DEVICE_ID,
                "device_class": "power",
                "unit": "W",
            },
            {
                "entity_id": ENERGY_ENTITY,
                "device_id": HA_DEVICE_ID,
                "device_class": "energy",
                "unit": "kWh",
            },
        ]
        resolution = _resolve({"dev_hot_tub": HA_DEVICE_ID}, entities=entities)

        assert resolution.state_entity_ids("dev_hot_tub") == ()
        assert resolution.devices_without_state_entity == (
            {
                "device_id": "dev_hot_tub",
                "link": HA_DEVICE_ID,
                "link_kind": "device",
                "reason": "only_measurement_entities",
                "detail": "2 candidate entities are CT/power/energy measurements",
            },
        )

    def test_a_device_missing_from_the_registry_is_reported(self) -> None:
        resolution = state_links.resolve_links(
            {"dev_hot_tub": "ffffffffffffffffffffffffffffffff"},
            entities=[],
            engine_devices=["dev_hot_tub"],
            registry_device_ids=[],
        )
        record = resolution.devices_without_state_entity[0]
        assert record["reason"] == "device_not_in_registry"

    def test_an_entity_id_link_still_resolves(self) -> None:
        """Backward compatibility: existing installs store entity ids."""
        resolution = _resolve({"dev_hot_tub": STATE_ENTITY})

        assert resolution.state_entity_ids("dev_hot_tub") == (STATE_ENTITY,)
        assert resolution.devices_without_state_entity == ()

    def test_a_measurement_entity_link_is_refused_and_reported(self) -> None:
        resolution = _resolve({"dev_hot_tub": POWER_ENTITY})
        assert resolution.state_entity_ids("dev_hot_tub") == ()
        assert resolution.devices_without_state_entity[0]["reason"] == "measurement_entity"

    def test_a_link_to_a_non_state_domain_is_reported(self) -> None:
        resolution = _resolve({"dev_hot_tub": "automation.turn_on_tub"})
        record = resolution.devices_without_state_entity[0]
        assert record["reason"] == "not_a_state_domain"

    def test_an_unrecognised_link_value_is_reported(self) -> None:
        resolution = _resolve({"dev_hot_tub": "hot tub"})
        record = resolution.devices_without_state_entity[0]
        assert record["reason"] == "unrecognised_link"

    def test_disabled_entities_are_not_selected(self) -> None:
        entities = [
            {**_registry()[0], "disabled": True},
        ]
        resolution = _resolve({"dev_hot_tub": HA_DEVICE_ID}, entities=entities)
        assert resolution.state_entity_ids("dev_hot_tub") == ()
        assert resolution.devices_without_state_entity[0]["reason"] == "no_state_entities"

    def test_a_link_to_an_engine_device_that_does_not_exist_is_reported(self) -> None:
        resolution = _resolve({"dev_missing": HA_DEVICE_ID})
        record = resolution.devices_without_state_entity[0]
        assert record["device_id"] == "dev_missing"
        assert record["reason"] == "engine_device_not_in_topology"

    def test_the_signature_is_stable_and_order_independent(self) -> None:
        first = _resolve({"dev_hot_tub": HA_DEVICE_ID})
        second = _resolve(
            {"dev_hot_tub": HA_DEVICE_ID},
            entities=list(reversed(_registry())),
        )
        assert state_links.state_link_signature(first) == state_links.state_link_signature(
            second
        )


# --------------------------------------------------------------------------- #
# Serialisation: the resolved ids travel in the topology metadata
# --------------------------------------------------------------------------- #


def _topology_payload() -> dict[str, Any]:
    return {
        "version": 3,
        "status": "candidate",
        "channels": [
            {"ct_id": "ct_main", "entity_id": "sensor.panel_power"},
            {
                "ct_id": "ct_hot_tub",
                "entity_id": POWER_ENTITY,
                "device_id": "dev_hot_tub",
            },
        ],
        "devices": [
            {"device_id": "dev_main", "name": "Panel"},
            {"device_id": "dev_hot_tub", "name": "Hot tub", "metadata": {}},
        ],
    }


class TestMetadataCarriesTheResolvedIds:
    def test_resolved_ids_are_written_to_the_device_metadata(self) -> None:
        resolution = _resolve({"dev_hot_tub": HA_DEVICE_ID})
        updated, changed = state_links.apply_state_link_metadata(
            _topology_payload(), resolution
        )

        assert changed is True
        device = next(d for d in updated["devices"] if d["device_id"] == "dev_hot_tub")
        assert device["metadata"][state_links.LINK_STATE_ENTITIES_KEY] == [
            STATE_ENTITY,
            STATUS_ENTITY,
        ]
        # A device with no link is left untouched.
        other = next(d for d in updated["devices"] if d["device_id"] == "dev_main")
        assert state_links.LINK_STATE_ENTITIES_KEY not in (other.get("metadata") or {})

    def test_applying_twice_is_idempotent(self) -> None:
        resolution = _resolve({"dev_hot_tub": HA_DEVICE_ID})
        once, changed = state_links.apply_state_link_metadata(_topology_payload(), resolution)
        twice, changed_again = state_links.apply_state_link_metadata(once, resolution)

        assert changed is True
        assert changed_again is False
        assert twice == once

    def test_unlinking_removes_the_projections_but_not_evidence(self) -> None:
        """A removal must not leave a link-derived id looking like evidence."""
        resolution = _resolve({"dev_hot_tub": HA_DEVICE_ID})
        topology = _topology_payload()
        topology["devices"][1]["metadata"] = {"state_entity_ids": ["switch.evidence"]}
        applied, _ = state_links.apply_state_link_metadata(topology, resolution)

        emptied, changed = state_links.apply_state_link_metadata(
            applied, state_links.resolve_links({}, entities=[], engine_devices=["dev_hot_tub"])
        )
        assert changed is True
        metadata = emptied["devices"][1]["metadata"]
        assert state_links.LINK_STATE_ENTITIES_KEY not in metadata
        assert metadata["state_entity_ids"] == ["switch.evidence"]


# --------------------------------------------------------------------------- #
# End to end: the resolved ids make a transition mature into a sample
# --------------------------------------------------------------------------- #


class _StubHaSource:
    """Stands in for ``HASource``: emits events/samples into the core.

    No Home Assistant, no socket.  It mimics the one behaviour that matters:
    state-bearing entities become ``DeviceEvent``s and power entities become
    ``Measurement``s.
    """

    def __init__(self, core: Any) -> None:
        self.core = core
        self.events = 0

    async def state_change(self, entity_id: str, state: str, at: datetime) -> None:
        self.events += 1
        await self.core.on_event(_StubEvent(entity_id, state, at))

    async def power(self, entity_id: str, watts: float, at: datetime) -> None:
        from wattmosaic_engine.contracts import Measurement

        await self.core.on_sample(
            Measurement(
                entity_id=entity_id,
                ct_id=entity_id.split(".", 1)[-1],
                value=watts,
                unit="W",
                state_timestamp=at,
                received_at=at,
                quality="good",
                repeated=False,
                sequence=0,
            )
        )


class _StubEvent:
    __slots__ = ("entity_id", "new_state", "attributes", "occurred_at")

    def __init__(self, entity_id: str, new_state: str, occurred_at: datetime) -> None:
        self.entity_id = entity_id
        self.new_state = new_state
        self.attributes: dict[str, Any] = {}
        self.occurred_at = occurred_at


def _contract_topology():
    """The live shape: a CT power channel whose device declares no state entity."""
    from wattmosaic_engine.contracts import (
        CTChannel,
        CTType,
        DeviceTopology,
        Phase,
        Topology,
        TopologyStatus,
    )

    return Topology(
        status=TopologyStatus.CANDIDATE,
        channels=[
            CTChannel(
                ct_id="ct_main",
                entity_id="sensor.panel_power",
                ct_type=CTType.MAIN,
                phase=Phase.L1,
            ),
            CTChannel(
                ct_id="ct_hot_tub",
                entity_id=POWER_ENTITY,
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                parent_ct_id="ct_main",
                device_id="dev_hot_tub",
            ),
        ],
        devices=[
            DeviceTopology(device_id="dev_hot_tub", name="Hot tub", dedicated_ct_ids=["ct_hot_tub"]),
        ],
    )


def _ct_entities_by_device(topology) -> dict[str, list[str]]:
    channels = topology.channel_map()
    out: dict[str, list[str]] = {}
    for device in topology.devices:
        for ct_id in [*device.dedicated_ct_ids, *device.branch_ct_ids]:
            channel = channels.get(ct_id)
            if channel is not None and channel.entity_id:
                out.setdefault(device.device_id, []).append(channel.entity_id)
    return out


def _core_with_resolved_links(tmp_path):
    """Build the real engine + backend, with the link resolved integration-side.

    This mirrors production: the integration resolves the HA device id to
    explicit state entity ids, writes them into the topology metadata, and the
    engine only ever reads the topology.
    """
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.main import EngineCore, load_inference_backend
    from wattmosaic_engine.storage.database import Database

    topology = _contract_topology()
    resolution = state_links.resolve_links(
        {"dev_hot_tub": HA_DEVICE_ID},
        entities=_registry(),
        engine_devices=[d.device_id for d in topology.devices],
        ct_entity_ids=_ct_entities_by_device(topology),
    )
    payload, changed = state_links.apply_state_link_metadata(
        topology.model_dump(mode="json"), resolution
    )
    assert changed is True
    topology = topology.model_validate(payload)

    path = tmp_path / "intake.db"
    db = Database(path)
    db.save_topology(topology)
    config = EngineConfig(database_path=str(path), ha_token="unused", api_token="token")
    core = EngineCore(config, db)
    core.topology_cache = db.load_topology()
    core.inference_backend = load_inference_backend(config, db)
    return core, db


def _run_cycles(core, source, *, events: bool = True) -> None:
    """Drive the production loop at the documented 15 s cadence.

    No sleep: simulated time is pinned in the aligner, exactly as the existing
    end-to-end intake tests do.
    """
    import wattmosaic_engine.synchronization.aligner as aligner_module

    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    event_at = base + timedelta(seconds=150)
    original = aligner_module.utcnow
    try:
        state: str | None = None
        for index in range(70):
            at = base + timedelta(seconds=15 * index)
            aligner_module.utcnow = lambda at=at: at
            watts = 60.0 if at < event_at else 260.0
            asyncio.run(source.power(POWER_ENTITY, watts, at))
            if events:
                new_state = "on" if at >= event_at else "off"
                if new_state != state:
                    asyncio.run(source.state_change(STATE_ENTITY, new_state, at))
                    state = new_state
            # The main CT must exist or the matrix has no row for the panel.
            asyncio.run(source.power("sensor.panel_power", 1000.0 + watts, at))
            asyncio.run(core.run_inference_once())
    finally:
        aligner_module.utcnow = original


def test_a_resolved_state_transition_matures_into_a_sample(tmp_path) -> None:
    """The invariant that would have caught the live defect.

    A state change on a device whose link was an HA *device* id must become a
    learning sample once the documented window (~68 s = 4×15.7 s + 5 s) has
    elapsed.  Before the fix the event never left ``power_events_not_state``, so
    ``samples_seen`` stayed at 0 forever.
    """
    core, db = _core_with_resolved_links(tmp_path)
    try:
        source = _StubHaSource(core)
        _run_cycles(core, source)

        # The state entity was recognised as state, not as a measurement.
        assert core.device_states["dev_hot_tub"] == "on"
        assert core.power_events_not_state == 0
        assert core.unlinked_events_ignored == 0

        rows = db.learning_samples("dev_hot_tub", accepted_only=False)
        assert rows, "a resolved state transition produced no learning sample"
        assert rows[-1]["rejection_reason"] == "ambiguous_topology"

        status = core.inference_backend.learning_status()
        assert status["samples_seen"] >= 1
        # The invariant: seen > 0 implies rejected > 0 while the topology is a
        # candidate.  Zero seen *and* zero rejected is an empty offer stage.
        assert status["samples_rejected"] > 0
        assert status["dominant_rejection_reason"] == "ambiguous_topology"
        assert status["effective_state"] == "blocked"
    finally:
        db.close()


def test_a_power_only_change_does_not_create_a_sample(tmp_path) -> None:
    """The ``power_events_not_state`` bridge still holds.

    The power entity is linked for *measurement*; its availability changes are
    real observations but must never become device state, transitions or
    samples.
    """
    core, db = _core_with_resolved_links(tmp_path)
    try:
        source = _StubHaSource(core)
        import wattmosaic_engine.synchronization.aligner as aligner_module

        base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        original = aligner_module.utcnow
        try:
            for index in range(30):
                at = base + timedelta(seconds=15 * index)
                aligner_module.utcnow = lambda at=at: at
                asyncio.run(source.power(POWER_ENTITY, 60.0, at))
                asyncio.run(source.power("sensor.panel_power", 1000.0, at))
                if index == 5:
                    # The realistic reachable case: a bridge blip, not a watt
                    # reading.  It must still be refused as state.
                    asyncio.run(source.state_change(POWER_ENTITY, "unavailable", at))
                asyncio.run(core.run_inference_once())
        finally:
            aligner_module.utcnow = original

        assert core.power_events_not_state == 1
        assert core.device_states.get("dev_hot_tub") is None
        assert not core._pending_backend_events
        assert core.inference_backend.learning_status()["samples_seen"] == 0
        assert db.learning_samples("dev_hot_tub", accepted_only=False) == []
    finally:
        db.close()


def test_the_engine_accepts_an_explicit_state_entity_list(tmp_path) -> None:
    """C2: the engine must honour ``state_entity_ids`` from the topology alone.

    The engine has no HA registry.  It only reads metadata, so the resolved list
    is the whole contract; if this regresses the pipeline is dead again.
    """
    from wattmosaic_engine.device_links import (
        runtime_state_entities,
        state_entity_for_device,
    )

    core, db = _core_with_resolved_links(tmp_path)
    try:
        device = core.topology_cache.device_map()["dev_hot_tub"]
        assert runtime_state_entities(device) == [STATE_ENTITY, STATUS_ENTITY]
        assert state_entity_for_device(core.topology_cache, "dev_hot_tub", STATE_ENTITY)
        assert not state_entity_for_device(
            core.topology_cache, "dev_hot_tub", POWER_ENTITY
        )
    finally:
        db.close()


@pytest.mark.parametrize("domain", sorted(state_links.STATE_DOMAINS))
def test_every_documented_state_domain_is_accepted(domain: str) -> None:
    entities = [{"entity_id": f"{domain}.thing", "device_id": HA_DEVICE_ID, "domain": domain}]
    resolution = _resolve({"dev_hot_tub": HA_DEVICE_ID}, entities=entities)
    assert resolution.state_entity_ids("dev_hot_tub") == (f"{domain}.thing",)


# --------------------------------------------------------------------------- #
# The replay actually writes it (C1 at the boundary)
# --------------------------------------------------------------------------- #

flow = importlib.import_module(f"{PACKAGE_NAME}.flow_logic")


class _RecordingEngine:
    """Minimal client: the three methods the replay uses."""

    def __init__(self, topology: dict[str, Any]) -> None:
        self._topology = dict(topology)
        self.put_calls: list[dict[str, Any]] = []
        self.confirm_calls: list[Any] = []

    async def topology(self) -> dict[str, Any]:
        return dict(self._topology)

    async def put_topology(self, topology: dict[str, Any]) -> dict[str, Any]:
        self.put_calls.append(dict(topology))
        stored = dict(topology)
        stored["version"] = int(self._topology.get("version") or 0) + 1
        self._topology = stored
        return dict(stored)

    async def confirm_topology(self, version: Any = None) -> dict[str, Any]:
        self.confirm_calls.append(version)
        self._topology["status"] = "confirmed"
        return dict(self._topology)


def _link_payload() -> dict[str, Any]:
    """An engine topology with the hot tub's CT channel and no state entity."""
    return {
        "version": 4,
        "status": "candidate",
        "channels": [
            {"ct_id": "ct_main", "entity_id": "sensor.panel_power", "ct_type": "main"},
            {
                "ct_id": "ct_hot_tub",
                "entity_id": POWER_ENTITY,
                "ct_type": "dedicated_device",
                "device_id": "dev_hot_tub",
            },
        ],
        "devices": [
            {"device_id": "dev_hot_tub", "dedicated_ct_ids": ["ct_hot_tub"]},
        ],
    }


def _resolve_topology(topology: Any) -> Any:
    """The callable the coordinator hands the replay: topology -> resolution."""
    return state_links.resolve_links(
        {"dev_hot_tub": HA_DEVICE_ID},
        entities=_registry(),
        engine_devices=state_links.device_ids(topology),
        ct_entity_ids=state_links.ct_entities_by_device(topology),
    )


class TestTheReplayWritesTheResolvedIds:
    def test_a_link_only_install_is_still_replayed(self) -> None:
        """No CT assignment, one device link: the write must still happen.

        This is the live case.  ``CONF_CT_ASSIGNMENTS`` was empty (the wizard
        was never completed) while ``device_links`` named the hot tub, so the
        old early return meant the engine never learned a single state entity.
        """
        engine = _RecordingEngine(_link_payload())
        state = asyncio.run(
            flow.async_replay_topology(
                engine,
                {},
                confirmed=False,
                observed_version=4,
                state_links=_resolve_topology,
                link_signature="dev_hot_tub=" + HA_DEVICE_ID,
            )
        )

        assert len(engine.put_calls) == 1
        body = engine.put_calls[0]
        device = next(d for d in body["devices"] if d["device_id"] == "dev_hot_tub")
        assert device["metadata"]["link_state_entity_ids"] == [
            STATE_ENTITY,
            STATUS_ENTITY,
        ]
        # The measurement entity never becomes a state source.
        assert POWER_ENTITY not in device["metadata"]["link_state_entity_ids"]
        assert state.pushed is True
        assert state.reason == "state_links_missing"
        # The engine is told where the nesting came from.
        assert flow.TOPOLOGY_LINK_NOTE in body["notes"]

    def test_a_declaration_replay_carries_the_links_too(self) -> None:
        engine = _RecordingEngine(_link_payload())
        asyncio.run(
            flow.async_replay_topology(
                engine,
                {"ct_main": {"role": "main", "phase": "l1"}},
                confirmed=False,
                observed_version=4,
                state_links=_resolve_topology,
                link_signature="dev_hot_tub=" + HA_DEVICE_ID,
            )
        )
        assert len(engine.put_calls) == 1
        device = next(
            d for d in engine.put_calls[0]["devices"] if d["device_id"] == "dev_hot_tub"
        )
        assert device["metadata"]["link_state_entity_ids"] == [
            STATE_ENTITY,
            STATUS_ENTITY,
        ]

    def test_a_link_the_engine_already_carries_is_not_rewritten(self) -> None:
        """An in-sync install must stay silent: no write per poll."""
        payload = _link_payload()
        payload["devices"][0]["metadata"] = {
            state_links.LINK_STATE_ENTITIES_KEY: [STATE_ENTITY, STATUS_ENTITY]
        }
        engine = _RecordingEngine(payload)
        state = flow.TopologyPushState()
        signature = "dev_hot_tub=" + HA_DEVICE_ID

        for _ in range(3):
            asyncio.run(
                flow.async_replay_topology(
                    engine,
                    {},
                    observed_version=4,
                    state=state,
                    state_links=_resolve_topology,
                    link_signature=signature,
                )
            )

        assert engine.put_calls == []
        assert state.checked_links == signature
        # Not "no_assignments": a link *is* declared and resolved; it is simply
        # already on the engine.
        assert state.reason == "state_links_in_sync"

    def test_a_link_added_after_setup_reopens_the_check(self) -> None:
        """A version guard alone cannot see a newly declared link.

        The user links a device at 12:05; the engine's revision does not change,
        so without the input fingerprint the resolved ids would never be
        written and learning would stay silent forever.
        """
        payload = _link_payload()
        engine = _RecordingEngine(payload)
        state = flow.TopologyPushState()

        # First: no link at all.  The early return applies, nothing is read.
        asyncio.run(
            flow.async_replay_topology(
                engine, {}, observed_version=4, state=state, state_links=None
            )
        )
        assert engine.put_calls == []

        # Then the user adds the link; the same engine revision must still be
        # re-examined because the *inputs* changed.
        asyncio.run(
            flow.async_replay_topology(
                engine,
                {},
                observed_version=4,
                state=state,
                state_links=_resolve_topology,
                link_signature="dev_hot_tub=" + HA_DEVICE_ID,
            )
        )
        assert len(engine.put_calls) == 1

    def test_no_declaration_and_no_link_stays_offline(self) -> None:
        engine = _RecordingEngine(_link_payload())
        state = asyncio.run(flow.async_replay_topology(engine, {}, observed_version=4))
        assert engine.put_calls == []
        assert state.reason == "no_assignments"

    def test_a_link_only_write_never_demotes_a_confirmed_topology(self) -> None:
        """ADR-0008: a link must not silently disable learning.

        ``build_topology_update(confirmed=False)`` writes ``candidate``.  A
        link-only replay on an install whose rows were already verified would
        then re-block learning -- the write meant to enable samples would stop
        them.  The plan has to carry the engine's confirmation forward.
        """
        payload = _link_payload()
        payload["status"] = "confirmed"
        engine = _RecordingEngine(payload)

        asyncio.run(
            flow.async_replay_topology(
                engine,
                {},
                confirmed=False,
                observed_version=4,
                state_links=_resolve_topology,
                link_signature="dev_hot_tub=" + HA_DEVICE_ID,
            )
        )

        assert len(engine.put_calls) == 1
        assert engine.put_calls[0]["status"] == "confirmed"
        # Re-confirmed at the revision the PUT produced (4 -> 5), exactly as a
        # declaration replay on a confirmed engine does: never leave the engine
        # reporting candidate for a topology it had already confirmed.
        assert engine.confirm_calls == [5]

    def test_a_link_with_no_device_to_carry_it_is_reported_not_written(self) -> None:
        """Never invent a device: an empty topology cannot receive a link.

        The resolution refuses the link (the engine has no such device) and the
        replay writes nothing.  The refusal is what diagnostics surfaces; the
        alternative -- creating a device to hang the ids on -- would fabricate
        topology.
        """
        topology: dict[str, Any] = {
            "version": 1,
            "status": "candidate",
            "channels": [],
            "devices": [],
        }
        resolution = state_links.resolve_links(
            {"dev_hot_tub": HA_DEVICE_ID},
            entities=_registry(),
            engine_devices=state_links.device_ids(topology),
        )
        assert resolution.state_entities == {}
        assert resolution.devices_without_state_entity[0]["reason"] == (
            "engine_device_not_in_topology"
        )

        engine = _RecordingEngine(topology)
        state = asyncio.run(
            flow.async_replay_topology(
                engine,
                {},
                observed_version=1,
                state_links=_resolve_topology,
                link_signature="dev_hot_tub=" + HA_DEVICE_ID,
            )
        )
        assert engine.put_calls == []
        assert state.reason == "state_links_unresolved"

    def test_a_measurement_only_link_is_reported_as_unresolved(self) -> None:
        """The device exists; it just has no state-bearing entity.

        The replay must say so.  ``no_assignments`` would blame the wizard and
        send the operator to the wrong screen.
        """
        topology = _link_payload()
        topology["devices"] = [{"device_id": "dev_hot_tub"}]
        measurement_only = [
            row for row in _registry() if row["entity_id"] in (POWER_ENTITY, ENERGY_ENTITY)
        ]

        def resolve(payload: Any) -> Any:
            return state_links.resolve_links(
                {"dev_hot_tub": HA_DEVICE_ID},
                entities=measurement_only,
                engine_devices=state_links.device_ids(payload),
                ct_entity_ids=state_links.ct_entities_by_device(payload),
            )

        engine = _RecordingEngine(topology)
        state = asyncio.run(
            flow.async_replay_topology(
                engine,
                {},
                observed_version=4,
                state_links=resolve,
                link_signature="dev_hot_tub=" + HA_DEVICE_ID,
            )
        )
        assert engine.put_calls == []
        assert state.reason == "state_links_unresolved"


# --------------------------------------------------------------------------- #
# Wiring: the pieces are connected in the modules that own them
# --------------------------------------------------------------------------- #


class TestWiring:
    """``homeassistant`` is not installed, so wiring is checked by source."""

    def test_the_coordinator_resolves_links_from_the_entity_registry(self) -> None:
        source = (PACKAGE_DIR / "coordinator.py").read_text()
        assert "entity_registry as er" in source
        assert "er.async_get(self.hass)" in source
        assert "registry_rows(" in source
        assert "resolve_links(" in source
        # The resolved ids -- never the HA device id -- cross the boundary.
        assert "state_links=self.resolve_state_links" in source

    def test_the_replay_writes_only_the_link_metadata_key(self) -> None:
        source = (PACKAGE_DIR / "flow_logic.py").read_text()
        replay = source[source.index("async def async_replay_topology") :]
        replay = replay[: replay.index("\nasync def ")]
        assert "apply_state_link_metadata(" in replay
        assert "state_links_missing" in source
        # Evidence-owned lists are written by ``state_links``, not here.
        assert 'metadata["state_entity_ids"]' not in replay
        assert 'metadata["config_load_entity_ids"]' not in replay

    def test_diagnostics_surface_unresolvable_links(self) -> None:
        source = (PACKAGE_DIR / "diagnostics.py").read_text()
        assert "devices_without_state_entity" in source
        assert "resolve_state_links(" in source
        assert "link_state_entity_ids" in source
        view = state_links.LinkResolution().as_diagnostics()
        assert view["state_links"] == {}
        assert view["devices_without_state_entity"] == []

