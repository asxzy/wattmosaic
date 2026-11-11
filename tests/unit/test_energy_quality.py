"""Energy accrual quality gate, bucket-size totals and monitoring derivation.

What is proven here:

* a result that is not a measurement (``Source.UNKNOWN``, a placeholder watt
  value, ``quality_status`` of ``bad``/``missing``) accrues **no** energy — the
  kWh deliverable is never built from a number that does not describe the load;
* the accrual interval is clamped to the freshness of the newest measurement,
  so a CT that stops updating stops accruing energy: a 130 s-old reading
  contributes 0 kWh and a 60 s-old one at most 60 s;
* a measured result accrues exactly as before (the default
  ``measurement_age_s=None`` is unchanged behaviour), and restart/replay still
  cannot double count;
* ``Database.energy_total`` sums exactly one bucket size, mirroring the
  repository, so the same energy stored at two resolutions is counted once;
* ``derive_monitoring`` labels ``DEDICATED`` only when the graph yields a
  dedicated channel; sole occupancy of a branch CT is ``SHARED`` with
  ``sole_occupant=True`` and the same watts;
* ``confidence``'s ``DIRECT_MEASURED_FLOOR`` is a direct-measurement floor only,
  not the 55 % analytic fixed point of an inferred device with no models.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.aggregation import EnergyAggregator
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    InferenceResult,
    Phase,
    QualityStatus,
    Source,
    Topology,
)
from wattmosaic_engine.disaggregation.confidence import (
    DIRECT_MEASURED_FLOOR,
    ConfidenceInputs,
    compute_confidence,
)
from wattmosaic_engine.disaggregation.configuration import (
    derive_monitoring,
    effective_config,
)
from wattmosaic_engine.electrical.topology import build_graph
from wattmosaic_engine.storage import Database

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def db(tmp_db_path):
    database = Database(tmp_db_path)
    try:
        yield database
    finally:
        database.close()


def _result(
    device_id: str,
    power_w: float,
    at: datetime | None = None,
    *,
    source: Source = Source.INFERRED,
    quality: QualityStatus = QualityStatus.GOOD,
    provenance: dict | None = None,
) -> InferenceResult:
    return InferenceResult(
        device_id=device_id,
        timestamp=at or T0,
        power_w=power_w,
        source=source,
        confidence=0.8,
        quality_status=quality,
        provenance=dict(provenance or {}),
    )


def _feed(
    aggregator: EnergyAggregator,
    device_id: str,
    points: list[tuple[datetime, float]],
    *,
    age_s: float | None = None,
    **kwargs,
) -> None:
    for at, power in points:
        aggregator.add_result(
            _result(device_id, power, at, **kwargs), at, measurement_age_s=age_s
        )


def _constant(device_id: str, watts: float, seconds: int, step: float = 15.0):
    return [
        (T0 + timedelta(seconds=offset), watts)
        for offset in range(0, seconds + 1, int(step))
    ]


def _rows(db: Database, device_id: str) -> list[dict]:
    with db._lock:  # noqa: SLF001 - read-only inspection in tests
        rows = db._conn.execute(
            "SELECT * FROM energy_aggregates WHERE device_id=? ORDER BY bucket_start, bucket_s",
            (device_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# E1: results that are not measurements never accrue energy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": Source.UNKNOWN},
        {"provenance": {"power_w_is_placeholder": True}},
        {"quality": QualityStatus.BAD},
        {"quality": QualityStatus.MISSING},
    ],
    ids=["source_unknown", "placeholder_watts", "quality_bad", "quality_missing"],
)
def test_non_measurement_results_never_accrue_energy(db: Database, kwargs) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    points = _constant("dev", 1000.0, 120)

    # First a real baseline, then a whole minute of 1 kW that is NOT a
    # measurement.  It must add nothing.
    aggregator.add_result(_result("dev", 1000.0, points[0][0]), points[0][0])
    for at, power in points[1:]:
        aggregator.add_result(_result("dev", power, at, **kwargs), at)

    aggregator.flush(now=T0 + timedelta(seconds=600))

    assert aggregator.energy_total("dev") == 0.0
    assert aggregator.current_total_kwh("dev") == 0.0
    # No power sample was recorded either, so the bucket carries no fake mean.
    assert all(row["energy_kwh"] == 0.0 for row in _rows(db, "dev"))


def test_unknown_result_does_not_open_a_run(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    for at, power in _constant("dev", 2000.0, 120):
        aggregator.add_result(
            _result("dev", power, at, source=Source.UNKNOWN, provenance={"power_w_is_placeholder": True}),
            at,
        )
    assert aggregator.take_closed_runs() == []
    assert aggregator._devices["dev"].run is None  # noqa: SLF001 - nothing opened


def test_integration_passes_the_measurement_age_to_the_aggregator(db: Database) -> None:
    """The real call site must supply the age, or the clamp is dead code.

    A frozen CT that keeps being aligned would otherwise integrate forever; the
    aggregator only knows the age because ``integration._update_energy`` hands
    it over from ``InferenceResult.measurement_age_seconds``.
    """
    from wattmosaic_engine.integration import WattMosaicBackend

    backend = WattMosaicBackend(db, learning_enabled=False)
    assert backend.aggregator is not None
    result = _result("dev", 1000.0, T0)
    result.measurement_age_seconds = 130.0

    backend._update_energy([result])  # noqa: SLF001 - the unit under test

    assert backend.aggregator.energy_total("dev") == 0.0
    assert result.energy_kwh == pytest.approx(0.0)


def test_measured_result_still_accrues_identically(db: Database) -> None:
    """The gate must not change the arithmetic for a real measurement."""
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(aggregator, "dev", _constant("dev", 1000.0, 600))
    aggregator.flush(now=T0 + timedelta(seconds=700))

    assert aggregator.energy_total("dev") == pytest.approx(600 * 1000 / 3_600_000)


def test_configured_prior_and_model_estimated_are_still_integrated(db: Database) -> None:
    """E1 gates *placeholders*, not weaker-but-real evidence sources.

    ``configured_prior``/``model_estimated`` are assertions and fits, not
    fabricated zeros, so they keep contributing kWh exactly as before.
    """
    for source in (Source.CONFIGURED_PRIOR, Source.MODEL_ESTIMATED):
        aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
        _feed(aggregator, f"dev_{source.value}", _constant("x", 600.0, 300), source=source)
        aggregator.flush(now=T0 + timedelta(seconds=400))
        assert aggregator.energy_total(f"dev_{source.value}") == pytest.approx(
            300 * 600 / 3_600_000
        )


# --------------------------------------------------------------------------- #
# E1: accrual interval is clamped to measurement freshness
# --------------------------------------------------------------------------- #


def test_measurement_age_130s_accrues_zero_energy(db: Database) -> None:
    """A reading older than ``stale_after_s`` describes no part of the interval.

    This is the phantom-energy mechanism: a CT that goes ``unavailable`` keeps
    its last value alignable for up to ``stale_after_s``.  Before the clamp the
    frozen watts integrated into kWh.
    """
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(aggregator, "dev", _constant("dev", 1000.0, 300), age_s=130.0)
    aggregator.flush(now=T0 + timedelta(seconds=400))

    assert aggregator.energy_total("dev") == 0.0


def test_measurement_age_60s_accrues_at_most_60s(db: Database) -> None:
    """A 60 s-old reading can describe at most 60 s of a 120 s interval.

    The interval is *clamped*, not the whole series: with a 120 s gap between
    results and a reading already 60 s old, only the newest 60 s is integrated.
    """
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    aggregator.add_result(_result("dev", 1000.0, T0), T0, measurement_age_s=0.0)
    aggregator.add_result(
        _result("dev", 1000.0, T0 + timedelta(seconds=120)),
        T0 + timedelta(seconds=120),
        measurement_age_s=60.0,
    )
    aggregator.flush(now=T0 + timedelta(seconds=400))

    # 60 s of 1 kW, not 120 s.
    assert aggregator.energy_total("dev") == pytest.approx(60 * 1000 / 3_600_000)


def test_measurement_age_zero_is_unchanged(db: Database) -> None:
    fresh = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(fresh, "fresh", _constant("fresh", 1000.0, 300), age_s=0.0)
    fresh.flush(now=T0 + timedelta(seconds=400))

    legacy = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(legacy, "legacy", _constant("legacy", 1000.0, 300))
    legacy.flush(now=T0 + timedelta(seconds=400))

    assert fresh.energy_total("fresh") == pytest.approx(legacy.energy_total("legacy"))


def test_stale_reading_stops_accruing_at_the_staleness_limit(db: Database) -> None:
    """A frozen CT accrues nothing once its held value is ``stale_after_s`` old.

    This is the production defect: ``sensor.ensuite_in_floor_heating_energy``
    reached 2.041 kWh while its power sensor was ``unavailable``.  Here the
    value is held constant while its age grows; energy may accrue during the
    window the staleness policy still permits, then must stop dead.
    """
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    # 600 s of a frozen 2 kW reading; the measurement age grows with the clock.
    for offset in range(0, 601, 15):
        at = T0 + timedelta(seconds=offset)
        aggregator.add_result(
            _result("dev", 2000.0, at), at, measurement_age_s=float(offset)
        )
    aggregator.flush(now=T0 + timedelta(seconds=700))

    total = aggregator.energy_total("dev")
    # Only the first 105 s of the frozen value can be described: the interval
    # ending at age 105 s still fits inside the 120 s allowance, the next one
    # does not.
    assert total == pytest.approx(105 * 2000 / 3_600_000)


# --------------------------------------------------------------------------- #
# E1: restart / replay accounting is unchanged
# --------------------------------------------------------------------------- #


def test_replay_after_restart_does_not_double_count(db: Database) -> None:
    points = _constant("dev", 900.0, 900)
    first = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(first, "dev", points)
    first.flush(now=T0 + timedelta(seconds=1000))
    db.insert_inference_results([_result("dev", 900.0, at) for at, _ in points])
    expected = first.energy_total("dev")
    assert expected == pytest.approx(900 * 900 / 3_600_000)

    for _ in range(3):
        restarted = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
        restarted.recompute_incomplete_buckets(
            now=T0 + timedelta(seconds=2000), lookback_hours=24
        )
        assert restarted.energy_total("dev") == pytest.approx(expected)


def test_replay_skips_placeholder_history(db: Database) -> None:
    """Replayed history is gated exactly like live ingestion.

    A stored placeholder row must not become energy when a crash replay
    re-integrates it; otherwise a restart could invent kWh that never accrued
    live.
    """
    points = _constant("dev", 1000.0, 300)
    db.insert_inference_results(
        [
            _result(
                "dev",
                power,
                at,
                source=Source.UNKNOWN,
                provenance={"power_w_is_placeholder": True},
            )
            for at, power in points
        ]
    )
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    aggregator.recompute_incomplete_buckets(now=T0 + timedelta(seconds=400), lookback_hours=24)

    assert aggregator.energy_total("dev") == 0.0


# --------------------------------------------------------------------------- #
# E3: Database.energy_total sums one bucket size only
# --------------------------------------------------------------------------- #


def test_database_energy_total_counts_each_bucket_size_once(db: Database) -> None:
    db.upsert_energy_aggregate("dev", T0, 300, 0.25)
    db.upsert_energy_aggregate("dev", T0, 3600, 0.25)
    db.upsert_energy_aggregate("dev", T0 + timedelta(seconds=300), 300, 0.25)
    db.upsert_energy_aggregate("dev", T0 + timedelta(seconds=300), 3600, 0.25)

    # The same physical energy is stored once per resolution.  Summing every
    # row would report 1.0 kWh; the smallest series reports 0.5 kWh.
    assert db.energy_total("dev") == pytest.approx(0.5)
    assert db.energy_total("dev", 300) == pytest.approx(0.5)
    assert db.energy_total("dev", 3600) == pytest.approx(0.5)


def test_database_energy_total_prefers_the_smallest_bucket(db: Database) -> None:
    db.upsert_energy_aggregate("dev", T0, 60, 0.1)
    db.upsert_energy_aggregate("dev", T0, 300, 0.2)
    db.upsert_energy_aggregate("dev", T0, 3600, 0.3)

    # ``None`` picks MIN(bucket_s), not the first row inserted.
    assert db.energy_total("dev") == pytest.approx(0.1)


def test_database_energy_total_is_zero_without_rows(db: Database) -> None:
    assert db.energy_total("nothing") == 0.0
    assert db.energy_total("nothing", 300) == 0.0


# --------------------------------------------------------------------------- #
# E4: DEDICATED only when the graph yields a dedicated channel
# --------------------------------------------------------------------------- #


def _solo_branch_topology() -> Topology:
    return Topology(
        channels=[
            CTChannel(
                ct_id="br_solo",
                entity_id="sensor.solo",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
            )
        ],
        devices=[
            DeviceTopology(
                device_id="heater", branch_ct_ids=["br_solo"], phase=Phase.L1
            )
        ],
    )


def test_sole_occupant_branch_is_shared_not_dedicated() -> None:
    topology = _solo_branch_topology()
    graph = build_graph(topology)

    assert (
        derive_monitoring(topology.devices[0], graph, topology=topology)
        == "shared"
    )
    effective = effective_config("heater", topology=topology, graph=graph)
    assert effective.monitoring == "shared"
    assert effective.sole_occupant is True
    assert effective.allows_direct_measurement is False
    # The label narrows; the watts do not.  The row is still fully attributable,
    # exactly as it was when the device was mislabelled DEDICATED.
    assert effective.shared_unmodelled is False


def test_dedicated_ct_is_dedicated_and_not_sole_occupant_flag() -> None:
    topology = Topology(
        channels=[
            CTChannel(
                ct_id="ct_heater",
                entity_id="sensor.heater",
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                device_id="heater",
            )
        ],
        devices=[
            DeviceTopology(
                device_id="heater", dedicated_ct_ids=["ct_heater"], phase=Phase.L1
            )
        ],
    )
    graph = build_graph(topology)

    assert (
        derive_monitoring(topology.devices[0], graph, topology=topology)
        == "dedicated"
    )
    effective = effective_config("heater", topology=topology, graph=graph)
    assert effective.monitoring == "dedicated"
    assert effective.sole_occupant is False
    assert effective.allows_direct_measurement is True


def test_branch_shared_by_two_devices_stays_shared_without_the_flag() -> None:
    topology = Topology(
        channels=[
            CTChannel(
                ct_id="br", entity_id="sensor.br", ct_type=CTType.BRANCH, phase=Phase.L1
            )
        ],
        devices=[
            DeviceTopology(device_id="a", branch_ct_ids=["br"], phase=Phase.L1),
            DeviceTopology(device_id="b", branch_ct_ids=["br"], phase=Phase.L1),
        ],
    )
    graph = build_graph(topology)
    effective = effective_config("a", topology=topology, graph=graph)

    assert effective.monitoring == "shared"
    assert effective.sole_occupant is False


def test_sole_occupant_does_not_change_the_watts() -> None:
    """The relabel is a label: the same topology yields the same device power."""
    from wattmosaic_engine.disaggregation.engine import DisaggregationEngine
    from wattmosaic_engine.electrical.window import ChannelReading

    class _Window:
        def __init__(self, at, readings):
            self.at = at
            self.readings = readings
            self.cadence_s = 15.0
            self.previous_states = {}
            self.state_transitions = []
            self.metadata = {}

    topology = Topology(
        channels=[
            CTChannel(ct_id="m_l1", entity_id="s.m", ct_type=CTType.MAIN, phase=Phase.L1),
            CTChannel(
                ct_id="br_solo",
                entity_id="s.br",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
                parent_ct_id="m_l1",
            ),
        ],
        devices=[
            DeviceTopology(device_id="heater", branch_ct_ids=["br_solo"], phase=Phase.L1)
        ],
    )
    window = _Window(
        T0,
        {
            "m_l1": ChannelReading(channel_id="m_l1", value_w=1200.0, age_s=1.0),
            "br_solo": ChannelReading(channel_id="br_solo", value_w=800.0, age_s=1.0),
        },
    )
    batch = DisaggregationEngine().infer(
        window, device_states={}, topology=topology, models=None
    )
    result = next(r for r in batch.results if r.device_id == "heater")

    # A branch CT is not a direct measurement, and it was never one: the device
    # is still separated from its row.  What must not happen is a *change* in
    # the watt value because the label moved.
    assert result.source != Source.DIRECT_MEASURED
    assert result.monitoring_level == "shared"
    assert result.power_w == pytest.approx(800.0, abs=1.0)


# --------------------------------------------------------------------------- #
# E6: the 55 % literal is a direct-measurement floor, not the inferred score
# --------------------------------------------------------------------------- #


def test_direct_measured_floor_is_named_and_documented() -> None:
    assert pytest.approx(0.55) == DIRECT_MEASURED_FLOOR


# --------------------------------------------------------------------------- #
# E5: a 0 W direct path is explained, never left bare
# --------------------------------------------------------------------------- #


def _backend_with_db(db: Database):
    from wattmosaic_engine.integration import WattMosaicBackend

    return WattMosaicBackend(db, learning_enabled=False)


class _Window:
    """Minimal window accepted by ``coerce_window``."""

    def __init__(self, readings: dict[str, float], at: datetime, *, age_s: float = 1.0) -> None:
        from wattmosaic_engine.electrical.window import ChannelReading

        self.at = at
        self.readings = {
            key: ChannelReading(channel_id=key, value_w=value, age_s=age_s)
            for key, value in readings.items()
        }
        self.cadence_s = 15.0
        self.previous_states = {}
        self.state_transitions = []
        self.metadata = {}


def test_branch_ct_devices_are_reported_as_missing_direct_measurement(db: Database) -> None:
    """The branch-only case: 0 W direct, and the reason is stated.

    ``resolve_direct`` only reads ``DEDICATED_DEVICE`` channels.  A branch CT
    therefore never produces a direct reading, however alone the device is on
    the circuit.  The ``ct_type`` gate is deliberately untouched here; what the
    fix adds is the *explanation* so a dashboard can say why.
    """
    topology = _solo_branch_topology()
    backend = _backend_with_db(db)
    batch = backend.process(
        _Window({"br_solo": 800.0}, T0), device_states={}, topology=topology
    )

    gaps = batch.diagnostics["measurement_gaps"]
    assert gaps["direct_measured_count"] == 0
    assert gaps["missing_direct_count"] == 1
    assert gaps["missing_direct"]["heater"] == (
        "no_dedicated_device_ct:attribution_ct_types=branch"
    )
    # And the reconciliation says it out loud.
    assert any(
        note.startswith("no_direct_measurement:1 device(s)")
        for note in batch.reconciliation.explanation
    )


def test_dedicated_ct_produces_a_direct_measurement_count(db: Database) -> None:
    topology = Topology(
        channels=[
            CTChannel(ct_id="m_l1", entity_id="s.m", ct_type=CTType.MAIN, phase=Phase.L1),
            CTChannel(
                ct_id="ct_heater",
                entity_id="s.heater",
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                parent_ct_id="m_l1",
                device_id="heater",
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="heater", dedicated_ct_ids=["ct_heater"], phase=Phase.L1
            )
        ],
    )
    backend = _backend_with_db(db)
    batch = backend.process(
        _Window({"m_l1": 1000.0, "ct_heater": 400.0}, T0),
        device_states={},
        topology=topology,
    )

    gaps = batch.diagnostics["measurement_gaps"]
    assert gaps["direct_measured_count"] == 1
    assert gaps["missing_direct_count"] == 0
    assert not any(
        note.startswith("no_direct_measurement")
        for note in batch.reconciliation.explanation
    )


def test_stale_measurement_is_counted_as_no_fresh_measurement(db: Database) -> None:
    """A stale CT is a first-class reason, not an invisible 86 % score.

    The device still has an attribution row, so it is not a "missing direct"
    case; it is reported under ``no_fresh_measurement`` with the actual age.
    """
    topology = _solo_branch_topology()
    backend = _backend_with_db(db)
    # Age the only CT past the staleness limit.
    window = _Window({"br_solo": 800.0}, T0, age_s=200.0)

    batch = backend.process(window, device_states={}, topology=topology)

    gaps = batch.diagnostics["measurement_gaps"]
    assert gaps["no_fresh_measurement_count"] >= 1
    reason = gaps["no_fresh_measurement"]["heater"]
    assert reason.startswith("measurement_age_") or reason == "source_unknown"
    assert any(
        note.startswith("no_fresh_measurement:")
        for note in batch.reconciliation.explanation
    )


def test_measurement_gap_explanation_is_idempotent(db: Database) -> None:
    """Pinning the same explanation twice must not duplicate the note."""
    topology = _solo_branch_topology()
    backend = _backend_with_db(db)
    batch = backend.process(
        _Window({"br_solo": 800.0}, T0), device_states={}, topology=topology
    )
    from wattmosaic_engine.integration import WattMosaicBackend

    gaps = batch.diagnostics["measurement_gaps"]
    before = list(batch.reconciliation.explanation)
    WattMosaicBackend._explain_measurement_gaps(batch.reconciliation, gaps)
    assert batch.reconciliation.explanation == before


def test_direct_availability_names_the_branch_only_case(db: Database) -> None:
    """A branch-only install is ``no_dedicated_ct``, not a silent zero.

    C1 resolved to (b): a 0 W direct count is acceptable only when it is
    *explained*.  This topology has no ``DEDICATED_DEVICE`` channel at all, so
    nothing is capable of a direct reading and the verdict says so -- the case a
    dashboard must render as "0 W because these are branch circuits", never as a
    bare zero.
    """
    topology = _solo_branch_topology()
    backend = _backend_with_db(db)
    batch = backend.process(
        _Window({"br_solo": 800.0}, T0), device_states={}, topology=topology
    )

    gaps = batch.diagnostics["measurement_gaps"]
    assert gaps["direct_measured_count"] == 0
    assert gaps["direct_capable_count"] == 0
    assert gaps["direct_availability"] == "no_dedicated_ct"
    assert gaps["direct_unavailable_explained"] is True
    assert gaps["unexplained_direct_devices"] == []


def test_direct_availability_reports_a_capable_device_with_no_reading(db: Database) -> None:
    """A dedicated CT that produced nothing is ``explained_unavailable``.

    The channel exists, so the install *is* capable of a direct reading; the
    reading is missing because the CT was absent from this window.  That is the
    distinction the count alone could not express.
    """
    topology = Topology(
        channels=[
            CTChannel(ct_id="m_l1", entity_id="s.m", ct_type=CTType.MAIN, phase=Phase.L1),
            CTChannel(
                ct_id="ct_heater",
                entity_id="s.heater",
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                parent_ct_id="m_l1",
                device_id="heater",
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="heater", dedicated_ct_ids=["ct_heater"], phase=Phase.L1
            )
        ],
    )
    backend = _backend_with_db(db)
    batch = backend.process(
        _Window({"m_l1": 1000.0}, T0), device_states={}, topology=topology
    )

    gaps = batch.diagnostics["measurement_gaps"]
    assert gaps["direct_measured_count"] == 0
    assert gaps["direct_capable_count"] == 1
    assert gaps["direct_availability"] == "explained_unavailable"
    assert gaps["missing_direct"]["heater"].startswith("direct_reading_unusable")
    assert gaps["direct_unavailable_explained"] is True
    assert gaps["unexplained_direct_devices"] == []


def test_a_measured_dedicated_ct_is_reported_as_measured(db: Database) -> None:
    topology = Topology(
        channels=[
            CTChannel(ct_id="m_l1", entity_id="s.m", ct_type=CTType.MAIN, phase=Phase.L1),
            CTChannel(
                ct_id="ct_heater",
                entity_id="s.heater",
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                parent_ct_id="m_l1",
                device_id="heater",
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="heater", dedicated_ct_ids=["ct_heater"], phase=Phase.L1
            )
        ],
    )
    backend = _backend_with_db(db)
    batch = backend.process(
        _Window({"m_l1": 1000.0, "ct_heater": 400.0}, T0),
        device_states={},
        topology=topology,
    )

    gaps = batch.diagnostics["measurement_gaps"]
    assert gaps["direct_availability"] == "measured"
    assert gaps["direct_capable_count"] == 1
    assert gaps["unexplained_direct_devices"] == []


def test_direct_measured_floor_only_applies_to_direct_measurement() -> None:
    # 0.88 x 0.625 = 0.55 is the *analytic fixed point* of a degenerate inferred
    # input set (no model, no corroboration) — a different number that happens
    # to print the same.  Reproduce it without the floor to prove they are not
    # the same constant: an inferred source can never be lifted by the floor.
    degenerate = compute_confidence(
        ConfidenceInputs(
            source=Source.INFERRED,
            measurement_qualities=[QualityStatus.GOOD],
            measurement_ages_s=[1.0],
            supporting_ct_count=1,  # one CT: ct_support == 0.5
            model_quality=0.0,
            event_isolation=0.6,
            topology_status="candidate",
            residual_w=0.0,
        )
    )
    blend = (
        0.30 * 1.0  # measurement_quality
        + 0.15 * 0.5  # ct_support
        + 0.20 * 0.0  # model_quality
        + 0.15 * 0.6  # event_isolation
        + 0.10 * 0.60  # topology_certainty
        + 0.10 * 1.0  # residual_quality
    )
    assert blend == pytest.approx(0.625)
    assert degenerate.score == pytest.approx(0.88 * blend, abs=1e-4)
    assert degenerate.score == pytest.approx(0.55, abs=0.001)

    direct = compute_confidence(
        ConfidenceInputs(
            source=Source.DIRECT_MEASURED,
            measurement_qualities=[QualityStatus.GOOD],
            measurement_ages_s=[1.0],
            supporting_ct_count=2,
            model_quality=1.0,
            event_isolation=1.0,
            topology_status="confirmed",
        )
    )
    assert direct.score >= DIRECT_MEASURED_FLOOR

    unknown = compute_confidence(ConfidenceInputs(source=Source.UNKNOWN))
    assert unknown.score < DIRECT_MEASURED_FLOOR
