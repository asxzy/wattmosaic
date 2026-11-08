"""Tests for the orchestrator integration adapter (backend wiring).

Verifies that the real `DisaggregationEngine` is correctly exposed to `main.py`
through the `process()` contract, and that failures never escape.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    Phase,
    Reconciliation,
    Source,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.electrical.window import ChannelReading
from wattmosaic_engine.integration import BackendBatch, WattMosaicBackend, build_backend


class _Window:
    """Minimal window accepted by ``coerce_window``."""

    def __init__(self, readings: dict[str, float], at: datetime) -> None:
        self.at = at
        self.readings = {
            k: ChannelReading(channel_id=k, value_w=v, age_s=1.0)
            for k, v in readings.items()
        }
        self.cadence_s = 15.0
        self.previous_states = {}
        self.state_transitions = []
        self.metadata = {}


def _topology() -> Topology:
    return Topology(
        status=TopologyStatus.CANDIDATE,
        channels=[
            CTChannel(ct_id="m_l1", entity_id="sensor.m_l1", ct_type=CTType.MAIN, phase=Phase.L1),
            CTChannel(ct_id="ded_fridge", entity_id="sensor.fridge", ct_type=CTType.DEDICATED_DEVICE,
                      phase=Phase.L1, parent_ct_id="m_l1"),
        ],
        devices=[
            DeviceTopology(device_id="dev_fridge", name="Fridge", dedicated_ct_ids=["ded_fridge"]),
        ],
    )


def test_process_returns_backend_batch() -> None:
    backend = WattMosaicBackend(database=None)
    now = datetime.now(UTC)
    window = _Window({"m_l1": 500.0, "ded_fridge": 120.0}, now)
    batch = backend.process(window, device_states={}, topology=_topology())
    assert isinstance(batch, BackendBatch)
    assert isinstance(batch.reconciliation, Reconciliation)
    fridge = next((r for r in batch.results if r.device_id == "dev_fridge"), None)
    assert fridge is not None
    assert fridge.source == Source.DIRECT_MEASURED
    assert fridge.power_w == pytest.approx(120.0, abs=1.0)


def test_process_preserves_unknown_residual() -> None:
    backend = WattMosaicBackend(database=None)
    now = datetime.now(UTC)
    window = _Window({"m_l1": 1000.0, "ded_fridge": 120.0}, now)
    batch = backend.process(window, device_states={}, topology=_topology())
    # 1000 measured - 120 direct = 880 unattributed; must not be handed to fridge.
    assert batch.reconciliation.unknown_residual_w > 800.0
    fridge = next(r for r in batch.results if r.device_id == "dev_fridge")
    assert fridge.power_w == pytest.approx(120.0, abs=1.0)


def test_process_isolates_backend_failure(monkeypatch) -> None:
    backend = WattMosaicBackend(database=None)

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic solver explosion")

    monkeypatch.setattr(backend.engine, "infer", boom)
    now = datetime.now(UTC)
    batch = backend.process(_Window({"m_l1": 1.0}, now), device_states={}, topology=_topology())
    assert batch.results == []
    assert batch.reconciliation.quality_status == "bad"
    assert batch.diagnostics.get("error")
    # The reconciliation still exists so the facade never sees None.
    assert isinstance(batch.reconciliation, Reconciliation)


def test_build_backend_uses_config_staleness() -> None:
    class Cfg:
        stale_after_s = 90.0

    backend = build_backend(Cfg())
    assert backend.engine.max_measurement_age_s == 90.0

def test_backend_status_reports_no_registry_without_db() -> None:
    backend = WattMosaicBackend(database=None)
    status = backend.status()
    assert status["model_registry"] is False
    assert status["backend"] == "WattMosaicBackend"


def test_missing_measurements_do_not_crash() -> None:
    backend = WattMosaicBackend(database=None)
    now = datetime.now(UTC)
    batch = backend.process(_Window({}, now), device_states={}, topology=_topology())
    assert isinstance(batch, BackendBatch)
    assert all(r.source in ("unknown", "inferred", "model_estimated", "direct_measured") for r in batch.results)
    for result in batch.results:
        assert 0.0 <= result.confidence <= 1.0


# --------------------------------------------------------------------------- #
# Observation tracker wiring
#
# The synchroniser only exposes a point-in-time snapshot.  Unless the backend
# feeds a StepTracker, the CT step detector never sees a second sample and
# event-based inference is dead code in production.  These tests fail if the
# tracker is dropped from ``WattMosaicBackend``.
# --------------------------------------------------------------------------- #


def test_backend_installs_a_step_tracker() -> None:
    backend = WattMosaicBackend(database=None)
    assert backend.tracker is not None
    assert backend.engine.tracker is backend.tracker


def test_successive_cycles_build_tracker_history() -> None:
    backend = WattMosaicBackend(database=None)
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    for i, watts in enumerate((100.0, 140.0, 900.0)):
        window = _Window({"m_l1": 1000.0, "ded_fridge": watts}, start + timedelta(seconds=15 * i))
        backend.process(window, device_states={}, topology=_topology())

    history = backend.tracker.history("ded_fridge")
    assert [round(v, 1) for _, v in history] == [100.0, 140.0, 900.0]
    assert backend.tracker.cadence_s() == pytest.approx(15.0)


def test_tracker_enables_ct_step_detection_in_production_loop() -> None:
    """A real step in a dedicated CT must be detected using only past cycles."""
    backend = WattMosaicBackend(database=None)
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def run(watts: float, index: int):
        window = _Window({"m_l1": 1000.0, "ded_fridge": watts}, start + timedelta(seconds=15 * index))
        return backend.process(window, device_states={}, topology=_topology())

    # Warm up on a flat series so the MAD detector has a noise estimate.
    for i in range(4):
        batch = run(100.0, i)
    assert batch.diagnostics.get("ct_steps") in ({}, None) or not batch.diagnostics["ct_steps"]

    # Fridge turns on: +800 W on its dedicated CT.
    batch = run(900.0, 4)
    steps = batch.diagnostics.get("ct_steps") or {}
    assert "ded_fridge" in steps, f"step not detected; diagnostics={batch.diagnostics}"
    assert steps["ded_fridge"]["delta_w"] == pytest.approx(800.0, abs=1.0)

    fridge = next(r for r in batch.results if r.device_id == "dev_fridge")
    assert fridge.power_w > 800.0


# --------------------------------------------------------------------------- #
# Negative residual is diagnosed, never hidden
# --------------------------------------------------------------------------- #


def test_negative_residual_is_flagged_as_sample_vintage_skew() -> None:
    """Accounted > main is impossible in one instant: it must be explained.

    In a real deployment the mains and branch CTs are sampled at different
    times, so a snapshot can contain more attributed power than the main CT
    reports.  The engine must not clamp this away silently; it must state the
    cause so an operator does not read it as double counting.
    """
    backend = WattMosaicBackend(database=None)
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    # Branch CTs sampled "later" than the main: 900 W of devices but a 500 W main.
    window = _Window({"m_l1": 500.0, "ded_fridge": 900.0}, now)
    batch = backend.process(window, device_states={}, topology=_topology())

    rec = batch.reconciliation
    assert rec.unknown_residual_w < 0, "expected a negative residual for this skew"
    assert "negative_residual_sample_vintage_skew" in rec.explanation
    assert "negative_residual_sample_vintage_skew" in batch.diagnostics["warnings"]
    # The value itself is preserved, not clamped to zero.
    assert rec.unknown_residual_w == pytest.approx(-400.0, abs=1.0)


def test_positive_residual_is_not_flagged_as_skew() -> None:
    backend = WattMosaicBackend(database=None)
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    window = _Window({"m_l1": 1000.0, "ded_fridge": 120.0}, now)
    batch = backend.process(window, device_states={}, topology=_topology())
    assert batch.reconciliation.unknown_residual_w > 0
    assert "negative_residual_sample_vintage_skew" not in batch.reconciliation.explanation




# --------------------------------------------------------------------------- #
# Production energy wiring
#
# ``InferenceResult.energy_kwh`` is what the HA ``total_increasing`` sensor
# publishes.  Before this wiring existed the aggregator was never constructed,
# so the field was always ``None`` and the sensor never advanced.  These tests
# fail if the aggregator is dropped from ``WattMosaicBackend`` again.
# --------------------------------------------------------------------------- #


@pytest.fixture()
def wired_backend(tmp_db_path):
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        yield WattMosaicBackend(db, learning_enabled=False), db
    finally:
        db.close()


def test_backend_constructs_energy_aggregator(wired_backend) -> None:
    backend, _db = wired_backend
    assert backend.aggregator is not None
    assert backend.status()["energy_aggregation"] is True


def test_results_carry_a_running_cumulative_energy_total(wired_backend) -> None:
    backend, _db = wired_backend
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    totals = []
    for i in range(6):
        window = _Window(
            {"m_l1": 1000.0, "ded_fridge": 1000.0}, start + timedelta(seconds=15 * i)
        )
        batch = backend.process(window, device_states={}, topology=_topology())
        fridge = next(r for r in batch.results if r.device_id == "dev_fridge")
        assert fridge.energy_kwh is not None, "energy must never be null when wired"
        totals.append(fridge.energy_kwh)

    assert totals == sorted(totals), f"energy must be monotonic, got {totals}"
    assert totals[-1] > totals[0], f"energy must advance, got {totals}"
    # 5 intervals of 15 s at 1000 W = 75 000 W·s = 0.0208 kWh.
    assert totals[-1] == pytest.approx(75_000 / 3_600_000, rel=0.02)


def test_energy_is_not_null_even_when_no_aggregator_exists() -> None:
    """Without a database the field stays null rather than being invented."""
    backend = WattMosaicBackend(database=None)
    window = _Window({"m_l1": 1000.0, "ded_fridge": 120.0}, datetime.now(UTC))
    batch = backend.process(window, device_states={}, topology=_topology())
    fridge = next(r for r in batch.results if r.device_id == "dev_fridge")
    assert fridge.energy_kwh is None
    assert backend.status()["energy_aggregation"] is False


def test_flush_persists_buckets(wired_backend) -> None:
    backend, _db = wired_backend
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    for i in range(3):
        window = _Window(
            {"m_l1": 1000.0, "ded_fridge": 1000.0}, start + timedelta(seconds=15 * i)
        )
        backend.process(window, device_states={}, topology=_topology())

    buckets = backend.flush_energy(now=start + timedelta(seconds=60))

    assert buckets
    assert any(b.device_id == "dev_fridge" for b in buckets)
    assert backend.aggregator.energy_total("dev_fridge") > 0.0


def test_close_flushes_pending_energy(wired_backend) -> None:
    backend, _db = wired_backend
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    for i in range(3):
        window = _Window(
            {"m_l1": 1000.0, "ded_fridge": 1000.0}, start + timedelta(seconds=15 * i)
        )
        backend.process(window, device_states={}, topology=_topology())

    backend.close()

    assert backend.aggregator.energy_total("dev_fridge") > 0.0


# --------------------------------------------------------------------------- #
# Production learning wiring
#
# ``learning_active: true`` used to be advertised while no pipeline existed,
# so no sample was ever collected and ``model_estimated`` could never be
# non-zero.  These tests pin the pipeline into the production path.
# --------------------------------------------------------------------------- #


def test_backend_constructs_learning_pipeline(tmp_db_path) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        backend = WattMosaicBackend(db, learning_enabled=True)
        assert backend.learning is not None
        assert backend.learning_active is True
        assert backend.status()["learning"] is True
    finally:
        db.close()


def test_learning_disabled_does_not_advertise_activity(tmp_db_path) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        backend = WattMosaicBackend(db, learning_enabled=False)
        assert backend.learning is None
        assert backend.learning_active is False
        assert backend.status()["learning"] is False
    finally:
        db.close()


def _topology_confirmed() -> Topology:
    """The same topology after the user confirms it (ADR-0008).

    Confirmation is what allows learning: an unconfirmed topology means the
    association itself is uncertain, so every sample is correctly rejected as
    ``ambiguous_topology`` (covered by its own test below).
    """
    base = _topology()
    return base.model_copy(
        update={
            "status": TopologyStatus.CONFIRMED,
            "devices": [
                d.model_copy(update={"topology_status": TopologyStatus.CONFIRMED})
                for d in base.devices
            ],
        }
    )


def test_home_assistant_events_become_persisted_learning_samples(tmp_db_path) -> None:
    """An HA event whose entity the topology owns must yield a real sample.

    Samples only mature once the collector's post-event window has elapsed, so
    the test feeds cycles past the event before asserting.
    """
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        backend = WattMosaicBackend(db, learning_enabled=True)
        topology = _topology_confirmed()
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        # The device entity is the CT's own entity in this topology, so the
        # association is derived from the topology, never guessed from names.
        entity_id = "sensor.fridge"
        # Warm the collector's window history: flat 100 W, then 900 W.
        for i, watts in enumerate([100.0] * 10 + [900.0] * 40):
            window = _Window(
                {"m_l1": 1000.0, "ded_fridge": watts}, start + timedelta(seconds=15 * i)
            )
            events = []
            if i == 10:
                events = [
                    {
                        "entity_id": entity_id,
                        "new_state": "on",
                        "attributes": {},
                        "occurred_at": start + timedelta(seconds=15 * i),
                    }
                ]
            backend.process(
                window, device_states={}, topology=topology, events=events
            )

        samples = db.learning_samples("dev_fridge", accepted_only=False)
        assert samples, "the HA event must produce a persisted learning sample"
        accepted = [s for s in samples if s["accepted"]]
        assert accepted, [s["rejection_reason"] for s in samples]
        assert accepted[0]["delta_power_w"] == pytest.approx(800.0, abs=1.0)
        assert accepted[0]["supporting_ct_ids_json"] not in ("[]", "", None)
    finally:
        db.close()


def test_unconfirmed_topology_rejects_samples_with_its_reason(tmp_db_path) -> None:
    """A candidate topology may collect evidence but must not train on it."""
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        backend = WattMosaicBackend(db, learning_enabled=True)
        topology = _topology()  # status=candidate
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        for i, watts in enumerate([100.0] * 10 + [900.0] * 40):
            window = _Window(
                {"m_l1": 1000.0, "ded_fridge": watts}, start + timedelta(seconds=15 * i)
            )
            events = (
                [
                    {
                        "entity_id": "sensor.fridge",
                        "new_state": "on",
                        "attributes": {},
                        "occurred_at": start + timedelta(seconds=150),
                    }
                ]
                if i == 10
                else []
            )
            backend.process(window, device_states={}, topology=topology, events=events)

        samples = db.learning_samples("dev_fridge", accepted_only=False)
        assert samples, "the sample is still recorded so the reason is visible"
        assert all(not s["accepted"] for s in samples)
        assert {s["rejection_reason"] for s in samples} == {"ambiguous_topology"}
        # And nothing was trained from a rejected sample.
        assert backend.training_attempts == 0
    finally:
        db.close()


def test_learning_waits_for_the_post_event_window(tmp_db_path) -> None:
    """A sample must not be built before the post-event readings exist."""
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        backend = WattMosaicBackend(db, learning_enabled=True)
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        window = _Window({"m_l1": 1000.0, "ded_fridge": 100.0}, start)
        event = {
            "entity_id": "sensor.fridge",
            "new_state": "on",
            "attributes": {},
            "occurred_at": start,
        }
        backend.process(window, device_states={}, topology=_topology(), events=[event])

        # Same cycle: nothing mature, nothing persisted, event retained.
        assert db.learning_samples("dev_fridge", accepted_only=False) == []
        assert len(backend._pending_events) == 1  # noqa: SLF001 - internal contract

        # Once the window has elapsed the event matures, is consumed and a
        # sample row appears (rejected here only because the topology is
        # still ``candidate`` — the reason is recorded, not hidden).
        for i in range(1, 30):
            w = _Window(
                {"m_l1": 1000.0, "ded_fridge": 900.0}, start + timedelta(seconds=15 * i)
            )
            backend.process(w, device_states={}, topology=_topology())
        assert backend._pending_events == deque()  # noqa: SLF001
        assert backend.samples_collected >= 1
    finally:
        db.close()


def test_unowned_event_is_rejected_not_attributed(tmp_db_path) -> None:
    """An event no topology device claims must never become a sample."""
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        backend = WattMosaicBackend(db, learning_enabled=True)
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        for i in range(8):
            window = _Window(
                {"m_l1": 1000.0, "ded_fridge": 100.0 + 50.0 * i},
                start + timedelta(seconds=15 * i),
            )
            events = (
                [
                    {
                        "entity_id": "light.someone_elses_lamp",
                        "new_state": "on",
                        "attributes": {},
                        "occurred_at": start,
                    }
                ]
                if i == 4
                else []
            )
            backend.process(window, device_states={}, topology=_topology(), events=events)

        owned = db.learning_samples("dev_fridge", accepted_only=False)
        # Whatever is stored for the owned device must not mention the foreign
        # entity; nothing may be attributed to it at all.
        for row in owned:
            assert "someone_elses_lamp" not in str(row.get("notes_json") or "")
        assert "light.someone_elses_lamp" not in str(db.recent_device_events(since=start))
    finally:
        db.close()


def test_paused_learning_stops_collecting(tmp_db_path) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        backend = WattMosaicBackend(db, learning_enabled=True)
        backend.set_learning_active(False)
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        for i in range(8):
            window = _Window(
                {"m_l1": 1000.0, "ded_fridge": 100.0 + 50.0 * i},
                start + timedelta(seconds=15 * i),
            )
            events = (
                [
                    {
                        "entity_id": "sensor.fridge",
                        "new_state": "on",
                        "attributes": {},
                        "occurred_at": start,
                    }
                ]
                if i == 4
                else []
            )
            backend.process(window, device_states={}, topology=_topology(), events=events)

        assert db.learning_samples("dev_fridge", accepted_only=False) == []
        assert backend.samples_collected == 0
    finally:
        db.close()


def test_training_throttle_is_not_consumed_before_data_exists(tmp_db_path) -> None:
    """Regression: the throttle must not be armed by a cycle with no samples.

    ``_maybe_train`` used to stamp ``last_train_at`` on its first call, before
    any device had enough accepted samples.  With the production interval of one
    hour that suppressed training for an hour *after* the data finally arrived,
    so an install could run for a day and never fit a single model.
    """
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        backend = WattMosaicBackend(db, learning_enabled=True)
        assert backend.learning is not None
        backend.train_interval_s = 3600.0

        # One cycle with no data at all: nothing is ready, so nothing is armed.
        backend._maybe_train(_topology_confirmed())  # noqa: SLF001 - internal contract
        assert backend.last_train_at is None

        # Now supply enough on/off transitions for the fridge to clear
        # ``min_samples_for_model`` and train.
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        topology = _topology_confirmed()
        state = "off"
        for i in range(0, 400):
            # A real appliance cycle: 100 W idle, 900 W when on, one hour-ish
            # worth of 15 s ticks with a transition every 12th tick.
            cycles = i // 12
            on = cycles % 2 == 1
            watts = 900.0 if on else 100.0
            window = _Window(
                {"m_l1": 1000.0, "ded_fridge": watts}, start + timedelta(seconds=15 * i)
            )
            events = []
            if i % 12 == 0:
                state = "on" if on else "off"
                events = [{
                    "entity_id": "sensor.fridge",
                    "new_state": state,
                    "attributes": {},
                    "occurred_at": start + timedelta(seconds=15 * i),
                }]
            backend.process(window, device_states={}, topology=topology, events=events)

        assert backend.samples_accepted >= 12, backend.status()
        assert backend.training_attempts >= 1, backend.status()
        assert backend.last_train_at is not None
    finally:
        db.close()
