"""End-to-end: simulator data through the real engine data path.

These tests exercise the pieces that sit *between* the simulator and the
disaggregation engine, using the same contract objects production uses:

``simulator record`` -> ``Measurement`` -> ``SampleAligner`` -> ``AlignedWindow``
-> ``DataQualityEngine`` / ``DisaggregationEngine`` -> ``InferenceResult``.

No component here is optional: the modules exist, so nothing is skipped.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tools.simulator import REAL_HOME_TOPOLOGY, HouseholdScenario, SimulatorConfig
from tools.simulator.sensors import QualityLevel

TOPOLOGY = REAL_HOME_TOPOLOGY.to_contract_topology()


def _enum_value(value) -> str:
    """Contract models may hold either an enum member or its plain string."""
    return getattr(value, "value", value)



def _records(minutes: float = 15, interval: float = 15, seed: int = 5, **kwargs):
    return list(
        HouseholdScenario(
            SimulatorConfig(
                seed=seed, minutes=minutes, interval=interval, quality=kwargs.pop(
                    "quality", QualityLevel.GOOD
                ), **kwargs
            )
        ).records()
    )


def _measurement(record: dict, ct_id: str):
    from wattmosaic_engine.contracts import Measurement

    m = record["measurements"][ct_id]
    return Measurement(
        entity_id=m["entity_id"],
        ct_id=ct_id,
        value=float(m["value_w"]),
        unit="W",
        state_timestamp=dt.datetime.fromisoformat(m["state_timestamp"]),
        received_at=dt.datetime.fromisoformat(m["received_at"]),
        physical_timestamp=(
            dt.datetime.fromisoformat(m["physical_timestamp"])
            if m.get("physical_timestamp")
            else None
        ),
        quality=m["quality"],
        repeated=m["repeated"],
        sequence=m["sequence"],
    )


# --------------------------------------------------------------------------- #
# Synchroniser
# --------------------------------------------------------------------------- #


def test_aligner_builds_a_window_from_simulated_samples() -> None:
    from wattmosaic_engine.synchronization.aligner import SampleAligner

    aligner = SampleAligner(max_age_s=120.0)
    records = _records()
    fed = 0
    last = None
    for record in records:
        for ct_id in record["measurements"]:
            if ct_id in TOPOLOGY.channel_map():
                aligner.add(_measurement(record, ct_id))
                fed += 1
        last = record

    assert fed > 0
    at = dt.datetime.fromisoformat(last["timestamp"])
    window = aligner.window(at=at, max_age_s=120.0)
    assert window.channels
    # Every channel the topology knows and that was sampled appears.
    assert len(window.channels) == 36
    # Values match the last delivered measurement for a channel.
    ct_id, m = next(iter(last["latest_measurements"].items()))
    if ct_id in window.channels:
        assert window.channels[ct_id].value_w == pytest.approx(float(m["value_w"]))
    assert window.complete or window.missing_channels


def test_aligner_marks_repeated_samples_not_fresh() -> None:
    from wattmosaic_engine.synchronization.aligner import SampleAligner

    records = _records(minutes=30, quality=QualityLevel.POOR)
    aligner = SampleAligner(max_age_s=600.0)
    repeated_seen = 0
    for record in records:
        for ct_id in record["measurements"]:
            if ct_id in TOPOLOGY.channel_map():
                aligner.add(_measurement(record, ct_id))
                repeated_seen += int(bool(record["measurements"][ct_id]["repeated"]))
    assert repeated_seen > 0, "poor quality must produce repeated samples"

    window = aligner.window(
        at=dt.datetime.fromisoformat(records[-1]["timestamp"]), max_age_s=600.0
    )
    repeated = [c for c in window.channels.values() if c.repeated]
    assert repeated, "repeated samples must not be silently dropped"
    for channel in repeated:
        assert channel.fresh is False
    # None of the repeated channels may be used as fresh observations.
    assert not set(window.fresh_channels) & {c.channel for c in repeated}


def test_aligner_marks_old_samples_stale() -> None:
    from wattmosaic_engine.synchronization.aligner import SampleAligner

    records = _records(minutes=5)
    aligner = SampleAligner(max_age_s=10.0)
    for ct_id in records[0]["measurements"]:
        if ct_id in TOPOLOGY.channel_map():
            aligner.add(_measurement(records[0], ct_id))

    # Ask for the window an hour later: everything must be stale.
    later = dt.datetime.fromisoformat(records[-1]["timestamp"]) + dt.timedelta(hours=1)
    window = aligner.window(at=later, max_age_s=10.0)
    assert window.stale_channels
    assert set(window.fresh_channels) == set()


# --------------------------------------------------------------------------- #
# Data quality engine
# --------------------------------------------------------------------------- #


def test_quality_engine_scores_a_real_window() -> None:
    from wattmosaic_engine.quality.engine import DataQualityEngine
    from wattmosaic_engine.synchronization.aligner import SampleAligner

    records = _records(minutes=15)
    aligner = SampleAligner(max_age_s=180.0)
    for record in records:
        for ct_id in record["measurements"]:
            if ct_id in TOPOLOGY.channel_map():
                aligner.add(_measurement(record, ct_id))

    at = dt.datetime.fromisoformat(records[-1]["timestamp"])
    window = aligner.window(at=at, max_age_s=180.0)
    health = aligner.channel_health(now=at)
    engine = DataQualityEngine(database=None)
    report = engine.evaluate(window, health, topology=TOPOLOGY)

    assert report.channels
    assert 0.0 <= report.overall_score <= 1.0
    for channel_id, channel in report.channels.items():
        assert channel_id in {c.ct_id for c in TOPOLOGY.channels}
        assert "score" in channel
        assert 0.0 <= float(channel["score"]) <= 1.0
    # A complete, fresh window must not be reported as globally bad.
    assert report.overall_score > 0.3


def test_quality_engine_penalises_stale_measurements() -> None:
    from wattmosaic_engine.quality.engine import DataQualityEngine
    from wattmosaic_engine.synchronization.aligner import SampleAligner

    records = _records(minutes=10)
    aligner = SampleAligner(max_age_s=180.0)
    for record in records:
        for ct_id in record["measurements"]:
            if ct_id in TOPOLOGY.channel_map():
                aligner.add(_measurement(record, ct_id))

    at = dt.datetime.fromisoformat(records[-1]["timestamp"])
    fresh = DataQualityEngine(database=None).evaluate(
        aligner.window(at=at, max_age_s=180.0),
        aligner.channel_health(now=at),
        topology=TOPOLOGY,
    )
    # Same data, but the window is now an hour old relative to "now".
    stale_at = at + dt.timedelta(hours=1)
    stale = DataQualityEngine(database=None).evaluate(
        aligner.window(at=stale_at, max_age_s=180.0),
        aligner.channel_health(now=stale_at),
        topology=TOPOLOGY,
    )
    assert stale.overall_score < fresh.overall_score
    assert stale.issues


# --------------------------------------------------------------------------- #
# Disaggregation engine over the real window type
# --------------------------------------------------------------------------- #


def _window_for(records, aligner, at, max_age_s=180.0):
    from wattmosaic_engine.contracts import QualityStatus
    from wattmosaic_engine.electrical.window import ChannelReading, ObservationWindow

    raw = aligner.window(at=at, max_age_s=max_age_s)
    readings = {
        ct_id: ChannelReading(
            channel_id=ct_id,
            value_w=channel.value_w,
            age_s=channel.age_seconds or 0.0,
            # The aligner stores quality as a plain string; the electrical
            # layer expects the ``QualityStatus`` enum.
            quality=QualityStatus(channel.quality),
            repeated=channel.repeated,
            polarization=TOPOLOGY.channel_map()[ct_id].polarity,
            expected_interval_s=15.7,
        )
        for ct_id, channel in raw.channels.items()
        if ct_id in TOPOLOGY.channel_map()
    }
    return ObservationWindow(at=at, readings=readings, cadence_s=15.7)




def _production_core(tmp_path, *, confirmed: bool = True):
    """Real EngineCore + backend, with state entities declared on the topology."""
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.contracts import TopologyStatus
    from wattmosaic_engine.main import EngineCore, load_inference_backend
    from wattmosaic_engine.storage.database import Database

    # The audit leaves the topology ``candidate``: confirmation is an explicit
    # user action (ADR-0008), so the rig starts there and upgrades only when the
    # test is about a confirmed install.
    topology = REAL_HOME_TOPOLOGY.to_contract_topology(confirmed=False)
    # The audit only maps *power* entities.  Declare the state entities that
    # describe each device so the production loop has something to link to.
    rigged = simulate_declared_state_entities(topology)
    if confirmed:
        rigged = rigged.model_copy(
            update={
                "status": TopologyStatus.CONFIRMED,
                "devices": [
                    d.model_copy(update={"topology_status": TopologyStatus.CONFIRMED})
                    for d in rigged.devices
                ],
            }
        )
    path = tmp_path / "production.db"
    db = Database(path)
    db.save_topology(rigged)
    config = EngineConfig(database_path=str(path), ha_token="unused", api_token="token")
    core = EngineCore(config, db)
    core.topology_cache = db.load_topology()
    core.inference_backend = load_inference_backend(config, db)
    return core, db


def simulate_declared_state_entities(topology):
    """Declare each simulator device's own HA state entity on the topology.

    The real audit only maps *power* CT entities, so nothing links a
    ``switch.*``/``climate.*`` event to a device and every learning sample is
    rejected.  Declaring the mapping is a product feature (a user confirms it);
    this rig supplies the mapping the simulator actually emits, which is kept in
    ``metadata["state_entity_ids"]``.  It is deliberately *not* guessed from
    names — see :mod:`wattmosaic_engine.device_links`.
    """
    from wattmosaic_engine.contracts import DeviceTopology
    from wattmosaic_engine.device_links import STATE_ENTITIES_KEY

    sim_entities = {
        device.device_id: device.ha_entity_id for device in REAL_HOME_TOPOLOGY.devices
    }
    devices = []
    for device in topology.devices:
        metadata = dict(device.metadata)
        entity_id = sim_entities.get(device.device_id)
        if entity_id:
            metadata[STATE_ENTITIES_KEY] = [entity_id]
        else:
            metadata.pop(STATE_ENTITIES_KEY, None)
        devices.append(DeviceTopology(**{**device.model_dump(), "metadata": metadata}))
    return topology.model_copy(update={"devices": devices})


def _feed_production(core, records, *, emit_events: bool = True):
    """Push simulator samples/events through EngineCore's real callbacks.

    Events are only emitted when a device's *state actually changes*, and they
    carry ``last_changed`` (the transition time), exactly like a Home Assistant
    ``state_changed`` event.  Emitting one per tick would give hundreds of
    duplicate "transitions" the timestamp of the very first one, which is both
    unrealistic and unlearnable: the sample collector has no baseline before it.
    """
    import asyncio

    from wattmosaic_engine.contracts import Measurement
    from wattmosaic_engine.synchronization import aligner as aligner_module

    last_changed: dict[str, str] = {}

    def changed_events(record) -> list[SimpleEvent]:
        out: list[SimpleEvent] = []
        for entity_id, state in (record.get("ha_states") or {}).items():
            changed = str(state.get("last_changed"))
            if last_changed.get(entity_id) == changed:
                continue
            last_changed[entity_id] = changed
            out.append(
                SimpleEvent(
                    entity_id=entity_id,
                    new_state=state.get("state"),
                    attributes=state.get("attributes") or {},
                    occurred_at=dt.datetime.fromisoformat(changed),
                )
            )
        return out

    async def push(record) -> None:
        for _ct_id, m in record["measurements"].items():
            await core.on_sample(
                Measurement(
                    entity_id=m["entity_id"],
                    # ``HASource`` derives the aligner key from the entity id
                    # (``sensor.foo_power`` -> ``foo_power``), and
                    # ``EngineCore._power_window`` joins topology channels on
                    # that key.  Reusing the topology ``ct_id`` here would hide a
                    # key mismatch that only exists in tests.
                    ct_id=str(m["entity_id"]).split(".", 1)[-1],
                    value=float(m["value_w"]),
                    unit="W",
                    state_timestamp=dt.datetime.fromisoformat(m["state_timestamp"]),
                    received_at=dt.datetime.fromisoformat(m["received_at"]),
                    quality=m["quality"],
                    repeated=m["repeated"],
                    sequence=m["sequence"],
                )
            )
        if emit_events:
            for event in changed_events(record):
                await core.on_event(event)

    original = aligner_module.utcnow
    try:
        for record in records:
            # The core reads wall-clock time to age samples and build windows.
            # Pin it to simulated time, otherwise every simulated reading looks
            # years stale and the whole window is discarded.
            simulated_now = dt.datetime.fromisoformat(record["timestamp"])
            aligner_module.utcnow = lambda at=simulated_now: at
            asyncio.run(push(record))
            asyncio.run(core.run_inference_once())
    finally:
        aligner_module.utcnow = original


class SimpleEvent:
    """The subset of ``DeviceEvent`` the core's callbacks read."""

    __slots__ = ("entity_id", "new_state", "attributes", "occurred_at")

    def __init__(self, *, entity_id, new_state, attributes, occurred_at) -> None:
        self.entity_id = entity_id
        self.new_state = new_state
        self.attributes = attributes
        self.occurred_at = occurred_at


def _declared_state_entity(core, device_id: str) -> str:
    """The state entity the topology declares for ``device_id``, if any."""
    from wattmosaic_engine.device_links import state_entity_ids

    device = next(d for d in core.topology_cache.devices if d.device_id == device_id)
    declared = state_entity_ids(device)
    assert declared, f"topology declares no state entity for {device_id}"
    return declared[0]


def test_device_states_are_keyed_by_device_id(tmp_path) -> None:
    """Regression: entity-id keys made every state lookup in the solver miss."""
    import asyncio

    core, db = _production_core(tmp_path)
    try:
        device = core.topology_cache.devices[0]
        entity_id = _declared_state_entity(core, device.device_id)
        event = SimpleEvent(
            entity_id=entity_id,
            new_state="on",
            attributes={},
            occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        asyncio.run(core.on_event(event))

        assert core.device_states[device.device_id] == "on"
        assert event.entity_id not in core.device_states, "entity id must not be used as a key"
    finally:
        db.close()


def test_undeclared_state_entity_is_not_attributed(tmp_path) -> None:
    """An entity no device declares must not reach inference or learning.

    Regression: undeclared entities used to be passed through under their own
    entity id.  With ~30 HA events/second against 40 declared links that filled
    the bounded learning buffer with noise, so *every* declared event was
    evicted before it could mature (1 439 samples, all ``missing_measurement``).
    The engine now counts them and keeps them out of ``device_states``.
    """
    import asyncio

    core, db = _production_core(tmp_path)
    try:
        before = core.unlinked_events_ignored
        asyncio.run(
            core.on_event(
                SimpleEvent(
                    entity_id="switch.a_neighbours_lamp",
                    new_state="on",
                    attributes={},
                    occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                )
            )
        )
        assert "a_neighbours_lamp" not in core.device_states
        assert "switch.a_neighbours_lamp" not in core.device_states
        # Counted, so a topology gap is visible instead of silent.
        assert core.unlinked_events_ignored == before + 1
        # And it never enters the inference buffer as a learning candidate.
        assert not core._pending_backend_events
    finally:
        db.close()

