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


def test_disaggregation_engine_runs_on_aligned_window() -> None:
    from wattmosaic_engine.disaggregation import DisaggregationEngine
    from wattmosaic_engine.synchronization.aligner import SampleAligner

    records = _records(minutes=20)
    aligner = SampleAligner(max_age_s=180.0)
    for record in records:
        for ct_id in record["measurements"]:
            if ct_id in TOPOLOGY.channel_map():
                aligner.add(_measurement(record, ct_id))

    at = dt.datetime.fromisoformat(records[-1]["timestamp"])
    window = _window_for(records, aligner, at)
    batch = DisaggregationEngine().infer(
        window, device_states={}, models=None, topology=TOPOLOGY
    )

    assert batch.results
    assert batch.reconciliation is not None
    known = {d.device_id for d in TOPOLOGY.devices}
    assert {r.device_id for r in batch.results} <= known
    for result in batch.results:
        assert result.power_w >= 0.0
        assert 0.0 <= result.confidence <= 1.0
        assert _enum_value(result.source) in (
            "direct_measured",
            "inferred",
            "model_estimated",
            "unknown",
        )
    recon = batch.reconciliation
    # Residual is reported and is not redistributed into device power.
    assert recon.unknown_residual_w == pytest.approx(
        recon.main_power_w
        - recon.direct_measured_w
        - recon.inferred_w
        - recon.model_estimated_w,
        abs=max(5.0, 0.02 * abs(recon.main_power_w)),
    )


def test_disaggregation_does_not_double_count_nested_panels() -> None:
    """Sub-panel feeds are nested under the main panel.

    Checked on ground truth: summing every ``main``-panel CT must exceed the
    true whole-home draw, while the estimator reports only the root mains.
    """
    from wattmosaic_engine.disaggregation import DisaggregationEngine
    from wattmosaic_engine.synchronization.aligner import SampleAligner

    records = _records(minutes=20)
    aligner = SampleAligner(max_age_s=180.0)
    for record in records:
        for ct_id in record["measurements"]:
            if ct_id in TOPOLOGY.channel_map():
                aligner.add(_measurement(record, ct_id))

    at = dt.datetime.fromisoformat(records[-1]["timestamp"])
    batch = DisaggregationEngine().infer(
        _window_for(records, aligner, at),
        device_states={},
        models=None,
        topology=TOPOLOGY,
    )
    truth = records[-1]["ground_truth"]
    main_cts = [
        c.ct_id for c in TOPOLOGY.channels if _enum_value(c.ct_type) == "main"
    ]
    latest = records[-1]["latest_measurements"]
    naive_nested_sum = sum(
        float(latest[c]["value_w"]) for c in main_cts if c in latest
    )
    root_sum = sum(
        float(latest[c]["value_w"])
        for c in ("ct_main_l1", "ct_main_l2")
        if c in latest
    )
    # The contract topology flattens the sub feed to a branch, so there is
    # exactly one root pair; the naive sum must not include a second panel.
    assert naive_nested_sum == pytest.approx(root_sum, abs=1e-6)
    assert batch.reconciliation.main_power_w == pytest.approx(root_sum, abs=1.0)
    # And the engine never claims more than the physical home consumed.  That
    # comparison cannot use the truth *at this instant*: the final record lands
    # on a load transition, so a root leg is up to one poll interval old and
    # still carries the previous load level.  Compare instead against the set of
    # true whole-home draws those samples could physically represent (the truth
    # over the covered sample-vintage span), which is what the engine is
    # entitled to report.
    last_ts = dt.datetime.fromisoformat(records[-1]["timestamp"])
    main_ages = [
        float(latest[c]["age_s"])
        for c in ("ct_main_l1", "ct_main_l2")
        if c in latest
    ]
    assert main_ages, "root mains must be present in the final record"
    # Record cadence, derived rather than assumed.
    cadence_s = (
        last_ts - dt.datetime.fromisoformat(records[-2]["timestamp"])
    ).total_seconds()
    assert cadence_s > 0.0
    vintage_span_s = max(main_ages) + cadence_s * 1.5
    vintage = [
        record
        for record in records
        if 0.0
        <= (last_ts - dt.datetime.fromisoformat(record["timestamp"])).total_seconds()
        <= vintage_span_s
    ]
    assert vintage, "sample-vintage span must cover at least one record"
    vintage_truths = [float(r["ground_truth"]["main_total_w"]) for r in vintage]
    # A stale leg may lag by one poll, so allow one interval of quantisation on
    # top of the range actually observed.
    vintage_tolerance = max(0.02 * max(vintage_truths), 100.0)
    assert batch.reconciliation.main_power_w <= max(vintage_truths) + vintage_tolerance, (
        batch.reconciliation.main_power_w,
        vintage_truths,
    )
    # Stronger, staleness-proof check: find the vintage instant the reported
    # main best explains and prove it tracks that record's root truth rather
    # than root + sub (the doubled figure a nested-CT bug would produce).
    best = min(
        vintage,
        key=lambda r: abs(
            batch.reconciliation.main_power_w - float(r["ground_truth"]["main_total_w"])
        ),
    )
    best_main = float(best["ground_truth"]["main_total_w"])
    best_doubled = best_main + float(best["ground_truth"]["panel_totals_w"]["sub"])
    reported_main = batch.reconciliation.main_power_w
    assert abs(reported_main - best_doubled) > abs(reported_main - best_main)
    assert reported_main > 0.0

    # Ground-truth side: the sub feed is real and non-trivial.  Its power is
    # already inside the main total, so any estimate that *added* the sub feed
    # to the mains would overshoot by exactly this much.
    sub_truth = float(truth["panel_totals_w"]["sub"])
    assert sub_truth > 0.0
    assert truth["ct_powers_w"]["ct_main_l1"] + truth["ct_powers_w"]["ct_main_l2"] == pytest.approx(
        truth["main_total_w"]
    )
    double_counted = truth["main_total_w"] + sub_truth
    assert double_counted > truth["main_total_w"] * 1.02, (
        "sub feed must be large enough that double counting is detectable"
    )
    # Compare against ground truth only at an instant whose whole sample-vintage
    # span is physically stable.  Freshness alone is not enough: a sample can be
    # 15 s old and still describe the *previous* load level if the home stepped
    # in between, and then root-truth vs root+sub is decided by the step, not by
    # any double-counting.  Requiring the truth to be flat over the span also
    # makes the comparison independent of the scenario realisation.
    cadence_s = (
        dt.datetime.fromisoformat(records[-1]["timestamp"])
        - dt.datetime.fromisoformat(records[-2]["timestamp"])
    ).total_seconds()
    assert cadence_s > 0.0

    def _vintage_truths(record: dict) -> list[float]:
        ages = [
            float(record["latest_measurements"][c]["age_s"])
            for c in ("ct_main_l1", "ct_main_l2")
            if c in record["latest_measurements"]
        ]
        if not ages or max(ages) > 16.5:
            return []
        span = max(ages) + cadence_s * 1.5
        at = dt.datetime.fromisoformat(record["timestamp"])
        return [
            float(other["ground_truth"]["main_total_w"])
            for other in records
            if 0.0
            <= (at - dt.datetime.fromisoformat(other["timestamp"])).total_seconds()
            <= span
        ]

    fresh = None
    for record in reversed(records):
        truths = _vintage_truths(record)
        if not truths:
            continue
        level = float(record["ground_truth"]["main_total_w"])
        if level <= 0.0:
            continue
        if min(truths) >= 0.98 * level and max(truths) <= 1.02 * level:
            fresh = record
            break
    assert fresh is not None, "no record had fresh samples over a stable main level"
    fresh_at = dt.datetime.fromisoformat(fresh["timestamp"])
    fresh_batch = DisaggregationEngine().infer(
        _window_for(records, aligner, fresh_at),
        device_states={},
        models=None,
        topology=TOPOLOGY,
    )
    fresh_truth = fresh["ground_truth"]
    fresh_doubled = fresh_truth["main_total_w"] + fresh_truth["panel_totals_w"]["sub"]
    # The engine's whole-home figure tracks the root truth, not the doubled one.
    assert abs(fresh_batch.reconciliation.main_power_w - fresh_doubled) > abs(
        fresh_batch.reconciliation.main_power_w - fresh_truth["main_total_w"]
    )


# --------------------------------------------------------------------------- #
# Learning sample rejection
# --------------------------------------------------------------------------- #


def test_learning_pipeline_rejects_low_quality_samples(tmp_path) -> None:
    """Bad learning samples must be rejected before they can train a model."""
    from wattmosaic_engine.contracts import LearningSample, SampleRejection
    from wattmosaic_engine.learning.sample_filter import (
        SampleEvidence,
        SampleFilter,
    )
    from wattmosaic_engine.learning.thresholds import LearningThresholds

    base = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    good = LearningSample(
        sample_id="good",
        device_id="hot_tub",
        captured_at=base,
        duration_s=120.0,
        baseline_power_w=10.0,
        event_power_w=2000.0,
        delta_power_w=1990.0,
        supporting_ct_ids=["ct_main_hot_tub_ct1", "ct_main_hot_tub_ct2"],
        quality_score=0.95,
    )
    bad = LearningSample(
        sample_id="bad",
        device_id="hot_tub",
        captured_at=base,
        duration_s=2.0,
        baseline_power_w=0.0,
        event_power_w=12.0,
        delta_power_w=12.0,
        supporting_ct_ids=["ct_main_hot_tub_ct1"],
        quality_score=0.1,
        # 80 W of noise dwarfs a 12 W step: not a trustworthy measurement.
        notes=["noise_w:80"],
    )
    evidence = SampleEvidence(
        now=base,
        baseline_samples=8,
        event_samples=8,
    )
    filter_ = SampleFilter(LearningThresholds())
    decision_good = filter_.evaluate(good, evidence)
    decision_bad = filter_.evaluate(bad, evidence)
    assert decision_good.accepted is True
    assert decision_bad.accepted is False
    assert decision_bad.reason is not None
    assert decision_bad.reason in tuple(SampleRejection)
    # The decision is annotatable onto the contract object without mutating it.
    annotated = decision_bad.apply(bad)
    assert annotated.accepted is False
    assert annotated.rejection_reason is decision_bad.reason
    assert bad.accepted is True, "apply() must not mutate the input sample"


def test_rejected_samples_are_not_counted_for_promotion(tmp_db_path) -> None:
    from wattmosaic_engine.contracts import LearningSample, SampleRejection
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        base = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
        db.insert_learning_sample(
            LearningSample(
                sample_id="accepted",
                device_id="hot_tub",
                captured_at=base,
                duration_s=60.0,
                baseline_power_w=5.0,
                event_power_w=1500.0,
                delta_power_w=1495.0,
                supporting_ct_ids=["ct_main_hot_tub_ct1"],
                quality_score=0.9,
                accepted=True,
            )
        )
        db.insert_learning_sample(
            LearningSample(
                sample_id="rejected",
                device_id="hot_tub",
                captured_at=base,
                duration_s=1.0,
                baseline_power_w=0.0,
                event_power_w=1.0,
                delta_power_w=1.0,
                quality_score=0.05,
                accepted=False,
                rejection_reason=SampleRejection.INSUFFICIENT_MAGNITUDE,
            )
        )
        assert db.count_learning_samples("hot_tub", accepted_only=True) == 1
        assert db.count_learning_samples("hot_tub", accepted_only=False) == 2
        accepted = db.learning_samples("hot_tub", accepted_only=True)
        assert [s["sample_id"] for s in accepted] == ["accepted"]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Production loop wiring (EngineCore -> backend -> state + learning + energy)
#
# These are the paths that were *dead* in production despite having unit tests:
#   * ``device_states`` was keyed by HA entity id, so every state lookup missed;
#   * no learning pipeline was ever constructed;
#   * no energy aggregator was ever constructed.
# The tests run the real ``EngineCore`` with the real backend against simulated
# Home Assistant state entities, so a regression cannot hide behind a mock.
# --------------------------------------------------------------------------- #


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


def test_production_loop_produces_monotonic_energy(tmp_path) -> None:
    """End-to-end: the value the HA ``total_increasing`` sensor publishes."""
    core, db = _production_core(tmp_path)
    try:
        records = _records(minutes=20)
        history: dict[str, list[float]] = {}
        for record in records:
            _feed_production(core, [record])
            for result in core.latest_results.values():
                if result.energy_kwh is not None:
                    history.setdefault(result.device_id, []).append(result.energy_kwh)

        assert history, "the production loop must publish energy for devices"
        for device_id, series in history.items():
            assert series == sorted(series), f"{device_id} energy decreased: {series}"
        assert any(series[-1] > series[0] for series in history.values())
        assert core.inference_backend.status()["energy_aggregation"] is True
    finally:
        db.close()


def test_production_loop_collects_learning_samples(tmp_path) -> None:
    core, db = _production_core(tmp_path)
    try:
        _feed_production(core, _records(minutes=40))

        status = core.inference_backend.status()
        assert status["learning"] is True
        assert status["samples_collected"] > 0, status
        assert status["samples_accepted"] > 0, status
        stored = [
            row
            for device in core.topology_cache.devices
            for row in db.learning_samples(device.device_id, accepted_only=False)
        ]
        assert stored, "no learning sample was persisted end-to-end"
        accepted = [row for row in stored if row["accepted"]]
        assert accepted, [row["rejection_reason"] for row in stored[:5]]
        for row in accepted:
            # Never a fabricated association: an accepted sample names its CTs.
            assert row["supporting_ct_ids_json"] not in ("[]", "", None)
            assert row["delta_power_w"] is not None
    finally:
        db.close()


def test_production_loop_trains_and_promotes_a_model(tmp_path) -> None:
    """The full learning loop: event -> sample -> fit -> validated model.

    This is the path that was completely dead before the production wiring: no
    learning pipeline existed, so ``model_estimated`` could never become
    non-zero.

    The backend throttles *automatic* training on wall-clock time
    (``train_interval_s``) so a busy install cannot retrain every cycle.  This
    test replays three simulated hours in a couple of real seconds, so the
    throttle is released between chunks — otherwise the whole simulated
    timeline would collapse into a single training pass and the loop's
    repeat-training behaviour would never run.
    """
    core, db = _production_core(tmp_path)
    try:
        backend = core.inference_backend
        # Train as soon as a device is ready rather than waiting an hour.
        backend.train_interval_s = 60.0

        records = _records(minutes=180)
        chunks = 6
        size = len(records) // chunks
        for index in range(chunks):
            start = index * size
            stop = len(records) if index == chunks - 1 else (index + 1) * size
            _feed_production(core, records[start:stop])
            # Emulate the wall-clock interval that separates these chunks in a
            # real install, so the training throttle behaves as it would live.
            backend.last_train_at = None

        status = backend.status()
        assert status["samples_accepted"] >= 12, status
        assert status["training_attempts"] >= 1, status
        assert status["models_promoted"] >= 1, status

        versions = [
            version
            for device in core.topology_cache.devices
            for version in db.model_versions(device.device_id)
        ]
        assert versions, "a promoted model must be persisted"
        assert any(
            str(v.get("status")) == "active" for v in versions
        ), [(v.get("device_id"), v.get("status")) for v in versions]
        # Models must never claim calibrated confidence (ADR-0007).
        for version in versions:
            params = version.get("parameters_json") or version.get("parameters")
            if isinstance(params, str):
                import json

                params = json.loads(params)
            if isinstance(params, dict) and "confidence_is_calibrated" in params:
                assert params["confidence_is_calibrated"] is False
    finally:
        db.close()


def test_candidate_topology_collects_but_never_trains(tmp_path) -> None:
    """Unconfirmed topology: samples are recorded *and rejected*, never fitted."""
    core, db = _production_core(tmp_path, confirmed=False)
    try:
        core.inference_backend.train_interval_s = 60.0
        _feed_production(core, _records(minutes=180))

        backend = core.inference_backend
        assert backend.status()["samples_collected"] > 0
        assert backend.status()["samples_accepted"] == 0
        assert backend.status()["training_attempts"] == 0

        stored = [
            row
            for device in core.topology_cache.devices
            for row in db.learning_samples(device.device_id, accepted_only=False)
        ]
        assert stored
        # Every row is rejected.  Samples whose measurements exist are rejected
        # *because* the topology is unconfirmed (``ambiguous_topology``); the
        # rest simply lacked a usable baseline/event pair.  Neither may train.
        assert all(not row["accepted"] for row in stored)
        assert "ambiguous_topology" in {row["rejection_reason"] for row in stored}
        assert not any(
            db.model_versions(device.device_id) for device in core.topology_cache.devices
        )
    finally:
        db.close()


def test_learning_active_reflects_the_real_pipeline(tmp_path) -> None:
    core, db = _production_core(tmp_path)
    try:
        assert core.health().learning_active is True
        assert core.inference_backend.learning is not None
    finally:
        db.close()


def test_pause_learning_command_reaches_the_backend(tmp_path) -> None:
    import asyncio

    from wattmosaic_engine.contracts import ServiceCommand

    core, db = _production_core(tmp_path)
    try:
        result = asyncio.run(core.command(ServiceCommand(command="pause_learning")))
        assert result.ok is True
        assert core.learning_active is False
        assert core.inference_backend.learning_active is False
        assert core.health().learning_active is False

        result = asyncio.run(core.command(ServiceCommand(command="resume_learning")))
        assert result.ok is True
        assert core.inference_backend.learning_active is True
    finally:
        db.close()
