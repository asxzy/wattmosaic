"""Unit tests for the data-quality engine: power scoring, energy domain,
grouped overlaps, energy counter resets, and event logging.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DataQualityReport,
    Measurement,
    Phase,
    Polarity,
    QualityStatus,
    Reconciliation,
    Topology,
)
from wattmosaic_engine.quality import DataQualityEngine, QualityThresholds, score_to_status
from wattmosaic_engine.quality.engine import OVERLAP_EXPLANATION
from wattmosaic_engine.storage import Database
from wattmosaic_engine.synchronization import SampleAligner

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _m(
    ct_id: str,
    value: float,
    *,
    seconds: float = 0.0,
    repeated: bool = False,
    unit: str = "W",
    quality: str = "good",
    sequence: int = 0,
) -> Measurement:
    recv = T0 + timedelta(seconds=seconds)
    return Measurement(
        entity_id=f"sensor.{ct_id}",
        ct_id=ct_id,
        value=value,
        unit=unit,  # type: ignore[arg-type]
        value_w=value if unit in ("W", "kW") else None,
        state_timestamp=recv,
        received_at=recv,
        repeated=repeated,
        quality=quality,  # type: ignore[arg-type]
        sequence=sequence,
    )


def _healthy_aligner(values: dict[str, list[float]], *, step: float = 15.0) -> SampleAligner:
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=120)
    for name, series in values.items():
        for i, v in enumerate(series):
            aligner.add(_m(name, v, seconds=i * step, sequence=i))
    return aligner


def _evaluate(
    aligner: SampleAligner,
    *,
    db=None,
    topology: Topology | None = None,
    reconciliation: Reconciliation | None = None,
    at_offset: float = 0.0,
    energy: bool = True,
):
    engine = DataQualityEngine(database=db, stale_after_s=120)
    now = T0 + timedelta(seconds=at_offset)
    window = aligner.window(
        at=now, channels=[c for c in aligner.channels()], max_age_s=120
    )
    health = aligner.channel_health(now=now)
    energy_window = aligner.energy_window(at=now, max_age_s=120) if energy else None
    energy_health = aligner.energy_health(now=now) if energy else None
    report = engine.evaluate(
        window,
        health,
        topology=topology,
        reconciliation=reconciliation,
        energy_window=energy_window,
        energy_health=energy_health,
    )
    return engine, report


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #


def test_score_to_status_boundaries() -> None:
    assert score_to_status(1.0) == QualityStatus.GOOD
    assert score_to_status(0.80) == QualityStatus.GOOD
    assert score_to_status(0.79) == QualityStatus.DEGRADED
    assert score_to_status(0.55) == QualityStatus.DEGRADED
    assert score_to_status(0.54) == QualityStatus.STALE
    assert score_to_status(0.30) == QualityStatus.STALE
    assert score_to_status(0.29) == QualityStatus.BAD


def test_thresholds_are_configurable() -> None:
    t = QualityThresholds(good_min=0.95, degraded_min=0.9, stale_min=0.5)
    assert score_to_status(0.94, t) == QualityStatus.DEGRADED
    assert score_to_status(0.4, t) == QualityStatus.BAD


# --------------------------------------------------------------------------- #
# Healthy channels
# --------------------------------------------------------------------------- #


def test_healthy_channel_scores_good() -> None:
    aligner = _healthy_aligner({"l1": [100.0, 101.0, 100.5, 101.2, 100.8]})
    _, report = _evaluate(aligner, at_offset=60)
    assert isinstance(report, DataQualityReport)
    assert report.overall_score >= 0.8
    assert report.channels["l1"]["status"] == "good"
    assert report.channels["l1"]["domain"] == "power"
    assert report.channels["l1"]["value_w"] == pytest.approx(100.8)


def test_missing_channel_is_missing_status() -> None:
    aligner = _healthy_aligner({"l1": [100.0]})
    engine = DataQualityEngine(stale_after_s=120)
    window = aligner.window(at=T0, channels=["l1", "l2"], max_age_s=120)
    report = engine.evaluate(window, aligner.channel_health(now=T0))
    assert report.channels["l2"]["status"] == "missing"
    assert report.channels["l2"]["score"] == 0.0
    assert any("missing_sample" in i for i in report.issues)


def test_stale_channel_is_penalized() -> None:
    aligner = _healthy_aligner({"l1": [100.0]})
    _, report = _evaluate(aligner, at_offset=1000)
    entry = report.channels["l1"]
    assert entry["status"] in ("stale", "bad")
    assert entry["score"] < 0.8
    assert any("stale_sample" in i for i in report.issues)


def test_repeated_sample_is_penalized() -> None:
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=120)
    aligner.add(_m("l1", 100.0, seconds=0, sequence=1))
    aligner.add(_m("l1", 100.0, seconds=15, repeated=True, sequence=2))
    _, report = _evaluate(aligner, at_offset=15)
    entry = report.channels["l1"]
    assert entry["repeated"] is True
    assert entry["score"] < 1.0
    assert any("repeated_sample" in i for i in entry["issues"])


def test_implausible_value_is_flagged() -> None:
    aligner = _healthy_aligner({"l1": [500_000.0, 500_001.0]})
    _, report = _evaluate(aligner, at_offset=15)
    assert any("implausible_value" in i for i in report.issues)
    assert report.channels["l1"]["score"] < 0.8


def test_excessive_noise_is_flagged() -> None:
    noisy = [100.0, 200.0, 100.0, 200.0, 100.0, 200.0, 100.0]
    aligner = _healthy_aligner({"l1": noisy})
    _, report = _evaluate(aligner, at_offset=90)
    assert any("excessive_noise" in i for i in report.issues)


def test_absent_entity_sensor_unavailable() -> None:
    aligner = SampleAligner(stale_after_s=60)
    engine = DataQualityEngine(stale_after_s=60)
    window = aligner.window(at=T0, channels=["ghost"], max_age_s=60)
    report = engine.evaluate(window, {}, unavailable=["sensor.ghost"])
    assert report.channels["ghost"]["status"] == "missing"
    assert any("sensor_unavailable" in i for i in report.issues)


def test_polarity_and_phase_penalties() -> None:
    aligner = _healthy_aligner({"l1": [50.0, 51.0, 50.0]})
    topology = Topology(
        channels=[
            CTChannel(
                ct_id="l1",
                entity_id="sensor.l1",
                ct_type=CTType.MAIN,
                phase=Phase.UNKNOWN,
                polarity=Polarity.INVERTED,
            )
        ]
    )
    _, report = _evaluate(aligner, topology=topology, at_offset=30)
    issues = report.channels["l1"]["issues"]
    assert any("polarity_inconsistent" in i for i in issues)
    assert any("phase_unknown" in i for i in issues)


def test_a_load_row_declared_normal_but_negative_is_reported() -> None:
    """A load cannot consume negative power: the CT is reversed or the declaration is wrong.

    Left unreported, the value is clipped to zero downstream and the circuit
    silently vanishes from every total while still claiming good quality.  This
    is the real case found in the reference installation: two branch rows read
    about -1270 W while declared ``normal``.
    """
    aligner = _healthy_aligner({"heater": [-1271.0, -1270.0, -1271.0]})
    topology = Topology(
        channels=[
            CTChannel(
                ct_id="heater",
                entity_id="sensor.heater",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
                polarity=Polarity.NORMAL,
            )
        ]
    )
    _, report = _evaluate(aligner, topology=topology, at_offset=30)
    issues = report.channels["heater"]["issues"]
    assert any("polarity_inconsistent" in i for i in issues)
    assert any("negative" in i for i in issues)


@pytest.mark.parametrize("ct_type", [CTType.BRANCH, CTType.DEDICATED_DEVICE])
def test_every_load_row_type_is_checked(ct_type: CTType) -> None:
    aligner = _healthy_aligner({"row": [-50.0, -50.0, -50.0]})
    topology = Topology(
        channels=[
            CTChannel(
                ct_id="row",
                entity_id="sensor.row",
                ct_type=ct_type,
                phase=Phase.L1,
                polarity=Polarity.NORMAL,
            )
        ]
    )
    _, report = _evaluate(aligner, topology=topology, at_offset=30)
    assert any("polarity_inconsistent" in i for i in report.channels["row"]["issues"])


def test_a_main_row_may_report_negative_power_without_a_polarity_penalty() -> None:
    """Negative power on a main is real export, not a reversed CT.

    Penalising it would flag every exporting installation as defective, so the
    new check is scoped to load rows only.
    """
    aligner = _healthy_aligner({"main": [-900.0, -905.0, -900.0]})
    topology = Topology(
        channels=[
            CTChannel(
                ct_id="main",
                entity_id="sensor.main",
                ct_type=CTType.MAIN,
                phase=Phase.L1,
                polarity=Polarity.NORMAL,
            )
        ]
    )
    _, report = _evaluate(aligner, topology=topology, at_offset=30)
    assert not any(
        "polarity_inconsistent" in i for i in report.channels["main"]["issues"]
    )


def test_an_inverted_load_row_with_a_negative_value_is_not_flagged() -> None:
    """A correctly declared reversed CT must not be reported as a defect."""
    aligner = _healthy_aligner({"row": [-400.0, -401.0, -400.0]})
    topology = Topology(
        channels=[
            CTChannel(
                ct_id="row",
                entity_id="sensor.row",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
                polarity=Polarity.INVERTED,
            )
        ]
    )
    _, report = _evaluate(aligner, topology=topology, at_offset=30)
    assert not any(
        "polarity_inconsistent" in i for i in report.channels["row"]["issues"]
    )


# --------------------------------------------------------------------------- #
# Energy domain
# --------------------------------------------------------------------------- #


def test_energy_channel_reported_in_kwh_not_watts() -> None:
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=120)
    aligner.add(_m("energy_month", 1425.0, unit="kWh", seconds=0))
    aligner.add(_m("energy_month", 1426.0, unit="kWh", seconds=15))
    _, report = _evaluate(aligner, at_offset=15)

    entry = report.channels["energy_month"]
    assert entry["domain"] == "energy"
    assert entry["unit"] == "kWh"
    assert entry["value_w"] is None
    assert entry["value_kwh"] == pytest.approx(1426.0)
    assert entry["status"] == "good"
    # No power-domain issue text for energy channels.
    for line in report.issues:
        if "energy_month" in line:
            assert " W" not in line
            assert "value_w" not in line


def test_energy_never_participates_in_power_overlap() -> None:
    """Regression: cumulative counters with equal values must not be compared as W."""
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=120)
    aligner.add(_m("a_this_month_energy", 1.425, unit="kWh", seconds=0))
    aligner.add(_m("b_this_month_energy", 1.425, unit="kWh", seconds=0))
    aligner.add(_m("c_this_week_energy", 1.425, unit="kWh", seconds=0))
    _, report = _evaluate(aligner, at_offset=0)
    assert not any("measurement_overlap" in i for i in report.issues)
    for name in ("a_this_month_energy", "b_this_month_energy", "c_this_week_energy"):
        assert report.channels[name]["domain"] == "energy"
        assert report.channels[name]["value_w"] is None


def test_energy_counter_reset_reported_in_kwh() -> None:
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=120)
    aligner.add(_m("meter", 100.0, unit="kWh", seconds=0))
    aligner.add(_m("meter", 101.0, unit="kWh", seconds=15))
    aligner.add(_m("meter", 0.5, unit="kWh", seconds=30))  # reset
    _, report = _evaluate(aligner, at_offset=30)
    entry = report.channels["meter"]
    assert entry["reset_count"] == 1
    assert entry["monotonic"] is False
    assert entry["max_drop_kwh"] == pytest.approx(100.5)
    assert any("energy_counter_reset" in i and "kWh" in i for i in report.issues)
    assert not any("energy_counter_reset" in i and " W" in i for i in report.issues)


def test_energy_wh_is_scored_in_kwh() -> None:
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=120)
    aligner.add(_m("meter_wh", 2500.0, unit="Wh", seconds=0))
    aligner.add(_m("meter_wh", 2600.0, unit="Wh", seconds=15))
    _, report = _evaluate(aligner, at_offset=15)
    entry = report.channels["meter_wh"]
    assert entry["unit"] == "kWh"
    assert entry["value_kwh"] == pytest.approx(2.6)


def test_energy_stale_is_flagged_in_kwh_terms() -> None:
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=30)
    aligner.add(_m("meter", 5.0, unit="kWh", seconds=0))
    _, report = _evaluate(aligner, at_offset=500)
    entry = report.channels["meter"]
    assert entry["status"] in ("stale", "bad")
    assert entry["value_w"] is None


# --------------------------------------------------------------------------- #
# Overlap grouping
# --------------------------------------------------------------------------- #


def test_overlap_cluster_is_one_grouped_issue() -> None:
    rooms = [
        f"room_{name}_power"
        for name in ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j")
    ]
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=120)
    for name in rooms:
        aligner.add(_m(name, 6.42, seconds=0))
    _, report = _evaluate(aligner, at_offset=0)

    overlap_issues = [i for i in report.issues if i.startswith("measurement_overlap")]
    assert len(overlap_issues) == 1, f"expected one grouped issue, got {overlap_issues}"
    line = overlap_issues[0]
    assert "size=10" in line
    for name in rooms:
        assert name in line
    assert "6.420 W" in line
    assert "duplicate source" in line
    assert "not be used as direct measurements" in line

    # Channels are kept, but reliability is reduced rather than zeroed.
    for name in rooms:
        entry = report.channels[name]
        assert entry["value_w"] == pytest.approx(6.42)
        assert 0.0 < entry["score"] < 1.0
        assert entry["reliability_factor"] < 1.0


def test_overlap_clusters_are_separate_per_value() -> None:
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=120)
    aligner.add(_m("a", 6.42, seconds=0))
    aligner.add(_m("b", 6.42, seconds=0))
    aligner.add(_m("c", 12.5, seconds=0))
    aligner.add(_m("d", 12.5, seconds=0))
    _, report = _evaluate(aligner, at_offset=0)
    overlaps = [i for i in report.issues if i.startswith("measurement_overlap")]
    assert len(overlaps) == 2


def test_duplicate_entity_binding_is_grouped() -> None:
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=120)
    aligner.add(_m("dup_a", 6.42, seconds=0))
    aligner.add(_m("dup_b", 6.42, seconds=0))
    topology = Topology(
        channels=[
            CTChannel(ct_id="dup_a", entity_id="sensor.same"),
            CTChannel(ct_id="dup_b", entity_id="sensor.same"),
        ]
    )
    _, report = _evaluate(aligner, topology=topology, at_offset=0)
    dup = [i for i in report.issues if i.startswith("duplicate_entity")]
    assert len(dup) == 1
    assert "size=2" in dup[0]


def test_distinct_values_are_not_overlap() -> None:
    aligner = _healthy_aligner({"a": [10.0], "b": [20.0], "c": [30.0]})
    _, report = _evaluate(aligner, at_offset=0)
    assert not any("measurement_overlap" in i for i in report.issues)


def test_zero_values_are_not_overlap() -> None:
    aligner = _healthy_aligner({"a": [0.0], "b": [0.0], "c": [0.0]})
    _, report = _evaluate(aligner, at_offset=0)
    assert not any("measurement_overlap" in i for i in report.issues)


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


def test_reconciliation_error_lowers_overall_score() -> None:
    aligner = _healthy_aligner({"l1": [100.0, 100.0, 100.0]})
    good = Reconciliation(reconciliation_error_ratio=0.01)
    _, report_good = _evaluate(aligner, reconciliation=good, at_offset=30)
    bad = Reconciliation(reconciliation_error_ratio=0.9)
    _, report_bad = _evaluate(aligner, reconciliation=bad, at_offset=30)
    assert report_bad.overall_score < report_good.overall_score
    assert any("reconciliation_error" in i for i in report_bad.issues)


# --------------------------------------------------------------------------- #
# Event logging
# --------------------------------------------------------------------------- #


def test_quality_events_logged_with_cooldown(tmp_path: Path) -> None:
    db = Database(tmp_path / "q.db")
    try:
        aligner = _healthy_aligner({"l1": [500_000.0]})
        engine = DataQualityEngine(database=db, stale_after_s=120)

        def run_once() -> DataQualityReport:
            window = aligner.window(at=T0, channels=aligner.channels(), max_age_s=120)
            return engine.evaluate(window, aligner.channel_health(now=T0))

        report = run_once()
        assert any("implausible_value" in i for i in report.issues)

        with db._lock:  # noqa: SLF001 - test introspection only
            rows = db._conn.execute(  # noqa: SLF001
                "SELECT code, severity, channel_id FROM data_quality_events"
            ).fetchall()
        codes = [r["code"] for r in rows]
        assert "implausible_value" in codes

        # Second evaluation within the cooldown must not duplicate events.
        before = len(rows)
        run_once()
        with db._lock:  # noqa: SLF001
            after = db._conn.execute(  # noqa: SLF001
                "SELECT COUNT(*) AS n FROM data_quality_events"
            ).fetchone()["n"]
        assert after == before
    finally:
        db.close()


def test_quality_engine_tolerates_db_failure() -> None:
    class BrokenDB:
        def log_quality_event(self, **_: object) -> None:
            raise RuntimeError("db down")

    aligner = _healthy_aligner({"l1": [500_000.0]})
    engine, report = _evaluate(aligner, db=BrokenDB(), at_offset=0)
    assert report.overall_score >= 0.0  # evaluation still completed


def test_status_for_and_last_report() -> None:
    aligner = _healthy_aligner({"l1": [100.0, 101.0]})
    engine, report = _evaluate(aligner, at_offset=15)
    assert engine.last_report is report
    assert engine.status_for("l1") == QualityStatus.GOOD
    assert engine.status_for("unknown") == QualityStatus.MISSING


def test_non_finite_value_is_bad() -> None:
    SampleAligner(max_age_s=10_000)
    # Non-finite is rejected by the aligner, so force the window directly.
    from wattmosaic_engine.contracts import QualityStatus as QS
    from wattmosaic_engine.synchronization.aligner import AlignedChannel, AlignedWindow

    ch = AlignedChannel(
        channel="l1",
        value_w=math.inf,
        age_seconds=0.0,
        quality="good",
        repeated=False,
        state_timestamp=T0,
        received_at=T0,
        entity_id="sensor.l1",
    )
    window = AlignedWindow(at=T0, max_age_s=120, channels={"l1": ch})
    engine = DataQualityEngine(stale_after_s=120)
    report = engine.evaluate(window, {})
    assert report.channels["l1"]["status"] == QS.BAD.value
    assert any("implausible_value" in i for i in report.issues)


def test_overlap_explanation_constant_present() -> None:
    assert "duplicate" in OVERLAP_EXPLANATION
    assert "direct measurements" in OVERLAP_EXPLANATION
