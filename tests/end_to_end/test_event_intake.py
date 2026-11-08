"""Regression tests for the production event-intake path (orchestrator-owned).

The passive real-HA run exposed a cluster of defects that no unit test had
covered, because each one only appears when the *whole* path runs against a
realistic event mix:

``HASource`` → ``EngineCore.on_event`` → learning buffer → ``SampleCollector``.

The observed symptom was 1 439 learning samples, every one rejected
``missing_measurement`` (``no_supporting_ct``), and no sample at all for the
eight devices that *do* have evidence-backed links.  Four independent defects
combined to produce it.  Each test below pins one of them so it cannot come
back.

Nothing here talks to Home Assistant.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from datetime import UTC, datetime, timedelta

import pytest
import wattmosaic_engine.synchronization.aligner as aligner_module

from tests.end_to_end.test_pipeline_e2e import (
    REAL_HOME_TOPOLOGY,
    SimpleEvent,
    simulate_declared_state_entities,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _core(tmp_path, *, confirmed: bool = True):
    """Real ``EngineCore`` + inference backend over the real-home topology."""
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.contracts import TopologyStatus
    from wattmosaic_engine.main import EngineCore, load_inference_backend
    from wattmosaic_engine.storage.database import Database

    topology = REAL_HOME_TOPOLOGY.to_contract_topology(confirmed=False)
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
    path = tmp_path / "intake.db"
    db = Database(path)
    db.save_topology(rigged)
    config = EngineConfig(database_path=str(path), ha_token="unused", api_token="token")
    core = EngineCore(config, db)
    core.topology_cache = db.load_topology()
    core.inference_backend = load_inference_backend(config, db)
    return core, db


def _declared_entity(core, device_id: str) -> str:
    from wattmosaic_engine.device_links import state_entity_ids

    device = next(d for d in core.topology_cache.devices if d.device_id == device_id)
    declared = state_entity_ids(device)
    assert declared, f"{device_id} declares no state entity"
    return declared[0]


# --------------------------------------------------------------------------- #
# Defect 1: undeclared entities flooded the bounded learning buffer
# --------------------------------------------------------------------------- #
def test_undeclared_events_never_enter_the_learning_buffer(tmp_path) -> None:
    """~30 HA events/s against 40 links starved the 512-event buffer.

    The maturity horizon is ``window_s + settle_s`` ≈ 68 s, so ~2 000 events
    have to survive to maturity.  A 512-entry buffer spans ~16 s and evicts
    every event 4× too early, which is why no declared event ever produced a
    sample.  Keeping undeclared entities out makes the buffer hold only the
    handful of events that can actually become samples.
    """
    core, db = _core(tmp_path)
    try:
        for index in range(50):
            asyncio.run(
                core.on_event(
                    SimpleEvent(
                        entity_id=f"binary_sensor.some_sensor_{index}",
                        new_state="on",
                        attributes={},
                        occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                    )
                )
            )
        assert len(core._pending_backend_events) == 0
        assert core.unlinked_events_ignored == 50
        assert not core.device_states
    finally:
        db.close()


def test_undeclared_events_are_not_persisted(tmp_path) -> None:
    """Undeclared entities must not grow the database.

    Home Assistant emits ~30 state changes/second across ~500 entities; only 40
    are declared link targets.  Persisting every undeclared change grew
    ``device_events`` at ~1.5 GB/day (99.98 % of all rows) for entities nothing
    ever reads, which violates the spec's "avoid unbounded database growth" and
    would eventually fill the disk on an unattended run.  They are counted, not
    written.
    """
    core, db = _core(tmp_path)
    try:
        for index in range(20):
            asyncio.run(
                core.on_event(
                    SimpleEvent(
                        entity_id=f"binary_sensor.neighbour_sensor_{index}",
                        new_state="on",
                        attributes={},
                        occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                    )
                )
            )
        core._drain_persist_queue_sync = None  # not used; queue must be empty
        assert core.persist_queue.stats()["depth"] == 0
        assert db._conn.execute("SELECT COUNT(*) c FROM device_events").fetchone()["c"] == 0
        assert core.unlinked_events_ignored == 20
    finally:
        db.close()


def test_declared_events_are_persisted(tmp_path) -> None:
    """The audit trail still records events for devices the topology owns."""
    core, db = _core(tmp_path)
    try:
        device = core.topology_cache.devices[0]
        entity_id = _declared_entity(core, device.device_id)
        asyncio.run(
            core.on_event(
                SimpleEvent(
                    entity_id=entity_id,
                    new_state="on",
                    attributes={},
                    occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                )
            )
        )
        # The event is queued for persistence even when it is only a first
        # observation: the *observation* is worth recording, the transition is not.
        assert core.persist_queue.stats()["depth"] == 1
    finally:
        db.close()


def test_declared_event_does_enter_the_learning_buffer(tmp_path) -> None:
    """The complement: a declared transition must still reach learning.

    The first observation only establishes state (the startup snapshot replay
    must not look like a transition), so a genuine change needs two events.
    """
    core, db = _core(tmp_path)
    try:
        device = core.topology_cache.devices[0]
        entity_id = _declared_entity(core, device.device_id)
        asyncio.run(
            core.on_event(
                SimpleEvent(
                    entity_id=entity_id,
                    new_state="off",
                    attributes={},
                    occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                )
            )
        )
        assert len(core._pending_backend_events) == 0
        asyncio.run(
            core.on_event(
                SimpleEvent(
                    entity_id=entity_id,
                    new_state="on",
                    attributes={},
                    occurred_at=dt.datetime(2026, 1, 1, 0, 0, 30, tzinfo=dt.UTC),
                )
            )
        )
        assert len(core._pending_backend_events) == 1
        assert core.unlinked_events_ignored == 0
        assert core.device_states[device.device_id] == "on"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Defect 2: the collector credited unlinked events to an arbitrary CT
# --------------------------------------------------------------------------- #
def _collector_with_two_circuits():
    """A collector that has observed a water_heater and a hot tub for ten minutes."""
    from wattmosaic_engine.learning.sample_collector import SampleCollector
    from wattmosaic_engine.synchronization.aligner import AlignedChannel, AlignedWindow

    def channel(name: str, value: float) -> AlignedChannel:
        return AlignedChannel(
            channel=name,
            value_w=value,
            age_seconds=0.0,
            quality="good",
            repeated=False,
            state_timestamp=None,
            received_at=None,
        )

    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    collector = SampleCollector()
    for index in range(40):
        collector.observe(
            AlignedWindow(
                at=base + timedelta(seconds=15 * index),
                max_age_s=120.0,
                channels={
                    "ct_water_heater": channel("ct_water_heater", 100.0 + index * 4),
                    "ct_hot_tub": channel("ct_hot_tub", 4000.0),
                },
            )
        )
    return collector, base


def test_unlinked_event_is_not_attributed_to_a_random_circuit() -> None:
    """Without a declared link the sample must carry no CT, not a guessed one.

    The fallback used to pick "whatever channels appear in the window", so an
    unrelated lamp event was measured against the water_heater CT and given a 5 W
    delta.  That is fabricated attribution: the number looks real and nothing
    downstream can tell it was invented.
    """
    collector, base = _collector_with_two_circuits()
    sample = collector.build(
        {
            "device_id": "switch.unrelated_lamp",
            "entity_id": "switch.unrelated_lamp",
            "new_state": "on",
            "attributes": {},
            "occurred_at": base + timedelta(seconds=150),
        }
    )
    assert sample is not None
    assert sample.supporting_ct_ids == []
    assert sample.delta_power_w == pytest.approx(0.0)
    assert "no_measurement" in sample.notes


def test_declared_event_is_still_measured_against_its_own_ct() -> None:
    """The fix must not disable the working path."""
    collector, base = _collector_with_two_circuits()
    sample = collector.build(
        {
            "device_id": "dev_water_heater",
            "entity_id": "binary_sensor.water_heater_zone_1_heating",
            "new_state": "on",
            "attributes": {"supporting_ct_ids": ["ct_water_heater"]},
            "occurred_at": base + timedelta(seconds=150),
        }
    )
    assert sample is not None
    assert sample.supporting_ct_ids == ["ct_water_heater"]
    assert sample.event_power_w != pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Defect 3: state transitions never reached the disaggregation engine
# --------------------------------------------------------------------------- #
def test_aligned_window_can_carry_transitions() -> None:
    """An ``AlignedWindow`` without transitions disables event inference.

    ``_collect_transitions`` reads ``window.state_transitions`` and falls back
    to ``window.previous_states``.  ``AlignedWindow`` carried neither, so
    ``coerce_window`` produced empty lists and every cycle reported zero events:
    state-transition inference was dead code in production even though the
    engine's own tests exercised it with hand-built windows.
    """
    from wattmosaic_engine.synchronization.aligner import AlignedWindow

    window = AlignedWindow(
        at=datetime(2026, 1, 1, tzinfo=UTC),
        max_age_s=120.0,
        channels={},
        previous_states={"dev_water_heater": "off"},
        state_transitions=(
            {
                "device_id": "dev_water_heater",
                "previous_state": "off",
                "new_state": "on",
                "occurred_at": None,
            },
        ),
    )
    assert window.previous_states == {"dev_water_heater": "off"}
    assert len(window.state_transitions) == 1


def test_transitions_survive_coercion_and_reach_the_engine() -> None:
    from wattmosaic_engine.disaggregation.engine import _collect_transitions
    from wattmosaic_engine.electrical.window import coerce_window
    from wattmosaic_engine.synchronization.aligner import AlignedWindow

    window = AlignedWindow(
        at=datetime(2026, 1, 1, tzinfo=UTC),
        max_age_s=120.0,
        channels={},
        previous_states={"dev_water_heater": "off"},
        state_transitions=(
            {
                "device_id": "dev_water_heater",
                "previous_state": "off",
                "new_state": "on",
                "occurred_at": None,
            },
        ),
    )
    transitions = _collect_transitions(coerce_window(window), {"dev_water_heater": "on"})
    assert list(transitions) == ["dev_water_heater"]
    assert transitions["dev_water_heater"].direction == "activation"


def test_core_records_a_declared_transition_for_the_next_window(tmp_path) -> None:
    """``on_event`` must queue the transition, not just the latest state."""
    core, db = _core(tmp_path)
    try:
        device = core.topology_cache.devices[0]
        entity_id = _declared_entity(core, device.device_id)
        asyncio.run(
            core.on_event(
                SimpleEvent(
                    entity_id=entity_id,
                    new_state="off",
                    attributes={},
                    occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                )
            )
        )
        asyncio.run(
            core.on_event(
                SimpleEvent(
                    entity_id=entity_id,
                    new_state="on",
                    attributes={},
                    occurred_at=dt.datetime(2026, 1, 1, 0, 0, 30, tzinfo=dt.UTC),
                )
            )
        )
        queued = [
            item for item in core._pending_transitions if item["device_id"] == device.device_id
        ]
        assert len(queued) == 1
        assert queued[0]["previous_state"] == "off"
        assert queued[0]["new_state"] == "on"
    finally:
        db.close()


def test_a_repeated_state_is_not_a_transition(tmp_path) -> None:
    """Attribute churn must not manufacture an event.

    The first observation of a device establishes its state; it is not a
    transition from anything.  Subsequent identical updates are attribute churn.
    """
    core, db = _core(tmp_path)
    try:
        device = core.topology_cache.devices[0]
        entity_id = _declared_entity(core, device.device_id)
        for _ in range(3):
            asyncio.run(
                core.on_event(
                    SimpleEvent(
                        entity_id=entity_id,
                        new_state="on",
                        attributes={},
                        occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                    )
                )
            )
        queued = [
            item for item in core._pending_transitions if item["device_id"] == device.device_id
        ]
        assert queued == [], "a repeated state is not a transition"
        assert core.device_states[device.device_id] == "on"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Defect 4: buffer eviction was silent
# --------------------------------------------------------------------------- #
def test_eviction_before_maturity_is_counted(tmp_path) -> None:
    """A full buffer drops the oldest event; that must be visible."""
    core, db = _core(tmp_path)
    try:
        device = core.topology_cache.devices[0]
        entity_id = _declared_entity(core, device.device_id)
        capacity = core._pending_backend_events.maxlen or 0
        assert capacity > 0
        # Establish the prior state, then alternate so every event is a real
        # transition and therefore enters the buffer.
        asyncio.run(
            core.on_event(
                SimpleEvent(
                    entity_id=entity_id,
                    new_state="off",
                    attributes={},
                    occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                )
            )
        )
        for index in range(capacity + 5):
            asyncio.run(
                core.on_event(
                    SimpleEvent(
                        entity_id=entity_id,
                        new_state="on" if index % 2 else "off",
                        attributes={},
                        occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                    )
                )
            )
        # The buffer starts empty, so the first ``capacity`` transitions fill it
        # without eviction.  One of the ``capacity + 5`` loop iterations repeats
        # the established "off" state and is not a transition, so four more
        # transitions follow — each evicting exactly one entry.
        assert core.events_evicted_before_maturity == 4
        assert len(core._pending_backend_events) == capacity
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# End to end: a linked device eventually produces an accepted sample
# --------------------------------------------------------------------------- #
def test_linked_event_produces_an_acceptable_sample_shape(tmp_path) -> None:
    """A mature declared event must yield a measurable, accepted-shaped sample.

    This is the property that was silently broken: the sample existed but had
    no supporting CT, so every row was rejected.  The candidate topology status
    still forces ``ambiguous_topology`` — that is correct and expected — but the
    *measurement* half must be present.
    """
    core, db = _core(tmp_path, confirmed=False)
    try:

        device = core.topology_cache.devices[0]
        entity_id = _declared_entity(core, device.device_id)
        ct_id = (device.dedicated_ct_ids or device.branch_ct_ids)[0]
        channel = next(c for c in core.topology_cache.channels if c.ct_id == ct_id)

        base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        key = core._aligner_key(channel)
        event_at = base + timedelta(seconds=150)

        original = aligner_module.utcnow
        try:
            # Drive a realistic sequence: feed one CT reading per 15 s cycle and
            # run inference each time, exactly as the production loop does.  The
            # collector needs several observed windows to form a baseline and an
            # event level; a single cycle could never produce a sample.
            last_state: str | None = None
            for index in range(60):
                at = base + timedelta(seconds=15 * index)
                value = 60.0 if at < event_at else 260.0
                aligner_module.utcnow = lambda at=at: at
                core.aligner.add(_measurement(channel.entity_id, key, value, at))
                state = "on" if at >= event_at else "off"
                if state != last_state:
                    asyncio.run(
                        core.on_event(
                            SimpleEvent(
                                entity_id=entity_id,
                                new_state=state,
                                attributes={},
                                occurred_at=at,
                            )
                        )
                    )
                last_state = state
                asyncio.run(core.run_inference_once())
        finally:
            aligner_module.utcnow = original

        rows = db._conn.execute(
            "SELECT device_id, supporting_ct_ids_json, rejection_reason, notes_json "
            "FROM learning_samples WHERE device_id = ?",
            (device.device_id,),
        ).fetchall()
        assert rows, "the declared event produced no learning sample at all"
        row = dict(rows[-1])
        assert row["supporting_ct_ids_json"] not in ("", "[]"), row
        assert "no_supporting_ct" not in row["notes_json"], row
        # Candidate topology is refused by policy, not for lack of measurement.
        assert row["rejection_reason"] == "ambiguous_topology", row
    finally:
        db.close()


def _measurement(entity_id: str, ct_id: str, value_w: float, at: datetime):
    from wattmosaic_engine.contracts import Measurement

    return Measurement(
        entity_id=entity_id,
        ct_id=ct_id,
        value=value_w,
        unit="W",
        state_timestamp=at,
        received_at=at,
        quality="good",
        repeated=False,
        sequence=0,
    )


# --------------------------------------------------------------------------- #
# Defect 29: a CT *power* entity overwrote the device's runtime state
# --------------------------------------------------------------------------- #
def test_a_power_entity_cannot_overwrite_the_device_runtime_state(tmp_path) -> None:
    """The device's state slot must hold runtime, not an availability blip.

    ``entity_links`` links a device's CT power entities too, because a learning
    sample needs to know which CTs measure it.  In production that meant the
    power entity's *events* were written into ``device_states[device_id]``.

    Numeric readings do not reach this path — ``HASource`` consumes them as
    measurements and returns early — but ``unavailable``, ``unknown`` and
    ``restored`` do.  Each CT bridge blip therefore replaced the real state with
    ``"unavailable"``, and the next genuine reading of the runtime entity looked
    like a transition back, so one blip manufactured *two* phantom transitions.
    The live ``device_events`` table holds 16 such rows across 16 CT power
    entities.

    A power entity is still persisted as an event — it is a real observation —
    but it may not claim to be the device's state.
    """
    from wattmosaic_engine.contracts import CTType

    core, db = _core(tmp_path)
    try:
        device = core.topology_cache.devices[0]
        entity_id = _declared_entity(core, device.device_id)
        asyncio.run(
            core.on_event(
                SimpleEvent(
                    entity_id=entity_id,
                    new_state="on",
                    attributes={},
                    occurred_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                )
            )
        )
        assert core.device_states[device.device_id] == "on"

        channels = core.topology_cache.channel_map()
        power_entities = [
            channels[cid].entity_id
            for cid in [*device.dedicated_ct_ids, *device.branch_ct_ids]
            if cid in channels and channels[cid].ct_type != CTType.MAIN
        ]
        assert power_entities, "fixture device must have its own CT"

        for value in ("unavailable", "unknown", "restored"):
            asyncio.run(
                core.on_event(
                    SimpleEvent(
                        entity_id=power_entities[0],
                        new_state=value,
                        attributes={},
                        occurred_at=dt.datetime(2026, 1, 1, 0, 0, 20, tzinfo=dt.UTC),
                    )
                )
            )
        assert core.device_states[device.device_id] == "on", (
            "a CT power entity overwrote the runtime state"
        )
        assert core.power_events_not_state == 3
        # No phantom transition was manufactured from availability blips.
        assert [
            item for item in core._pending_transitions if item["device_id"] == device.device_id
        ] == []
        # Re-reading the real state afterwards is still not a transition.
        asyncio.run(
            core.on_event(
                SimpleEvent(
                    entity_id=entity_id,
                    new_state="on",
                    attributes={},
                    occurred_at=dt.datetime(2026, 1, 1, 0, 0, 40, tzinfo=dt.UTC),
                )
            )
        )
        assert [
            item for item in core._pending_transitions if item["device_id"] == device.device_id
        ] == []
    finally:
        db.close()
