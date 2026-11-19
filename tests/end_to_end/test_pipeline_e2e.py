"""End-to-end: simulator data through the real alignment and quality path.

These tests exercise the pieces that sit between the simulator and inference:

``simulator record`` -> sample -> ``SampleAligner`` -> ``AlignedWindow``
-> ``DataQualityEngine``.

The legacy ``Measurement`` contract and contract ``Topology`` were removed with
the contract-3.0.0 cutover; :meth:`SampleAligner.add` is duck-typed, so the
simulator record is fed through a local sample type carrying exactly the fields
the aligner reads, and the simulator's own topology names the channels.

No component here is optional: the modules exist, so nothing is skipped.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from tools.simulator import REAL_HOME_TOPOLOGY, HouseholdScenario, SimulatorConfig
from tools.simulator.sensors import QualityLevel

#: The simulator's audited channels, keyed by ``ct_id``.
CHANNELS = REAL_HOME_TOPOLOGY.channel_map()


@dataclass(frozen=True)
class _Sample:
    """The duck-typed sample :meth:`SampleAligner.add` consumes."""

    entity_id: str
    ct_id: str
    value: float
    unit: str
    state_timestamp: dt.datetime
    received_at: dt.datetime
    physical_timestamp: dt.datetime | None
    quality: str
    repeated: bool
    sequence: int

    def effective_power_w(self) -> float:
        return self.value * (1000.0 if self.unit == "kW" else 1.0)


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


def _measurement(record: dict, ct_id: str) -> _Sample:
    m = record["measurements"][ct_id]
    return _Sample(
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
            if ct_id in CHANNELS:
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
            if ct_id in CHANNELS:
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
        if ct_id in CHANNELS:
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
            if ct_id in CHANNELS:
                aligner.add(_measurement(record, ct_id))

    at = dt.datetime.fromisoformat(records[-1]["timestamp"])
    window = aligner.window(at=at, max_age_s=180.0)
    health = aligner.channel_health(now=at)
    engine = DataQualityEngine(database=None)
    report = engine.evaluate(window, health)

    assert report.channels
    assert 0.0 <= report.overall_score <= 1.0
    for channel_id, channel in report.channels.items():
        assert channel_id in CHANNELS
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
            if ct_id in CHANNELS:
                aligner.add(_measurement(record, ct_id))

    at = dt.datetime.fromisoformat(records[-1]["timestamp"])
    fresh = DataQualityEngine(database=None).evaluate(
        aligner.window(at=at, max_age_s=180.0),
        aligner.channel_health(now=at),
    )
    # Same data, but the window is now an hour old relative to "now".
    stale_at = at + dt.timedelta(hours=1)
    stale = DataQualityEngine(database=None).evaluate(
        aligner.window(at=stale_at, max_age_s=180.0),
        aligner.channel_health(now=stale_at),
    )
    assert stale.overall_score < fresh.overall_score
    assert stale.issues
