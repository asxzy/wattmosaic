"""Honest learning accounting: accepted vs rejected, and what a user sees.

**The defect this file pins down**

``binary_sensor.wattmosaic_engine_active_learning`` reported ``on`` on a live
install while ``device_models`` was empty, ``model_estimated_power`` was 0 W
continuously, and no model had ever been promoted.  The cause was not a
modelling bug: the topology was a ``candidate`` (``topology_confirmed=false`` in
the config entry), so the filter stamped every sample ``ambiguous_topology`` and
rejected it — deliberately and correctly.

The pipeline was *running* and *accepting nothing*.  ``learning_active`` only
answered "does a loop exist?", so it could not tell those apart.

These tests pin the replacement contract:

* counters are monotonic and O(1) per sample (no per-sample storage growth);
* ``effective_state`` is ``learning`` only when samples are accepted;
* ``blocked`` carries the dominant reason and a plain-language remedy;
* ``off`` covers both a disabled and a paused pipeline;
* the dominant reason is correct on a mixed batch;
* the rejection *rules* are untouched (the same sample is still rejected).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from wattmosaic_engine.contracts import SampleRejection
from wattmosaic_engine.learning import (
    CONFIRM_REMEDY,
    LearningAccounting,
    LearningPipeline,
    build_learning_status,
)
from wattmosaic_engine.learning.reporting import (
    ALMOST_ALL_MIN_SAMPLES,
    MAX_REJECTION_LABELS,
    OTHER_LABEL,
)
from wattmosaic_engine.learning.sample_filter import SampleFilter
from wattmosaic_engine.storage import Database

from tests.unit.conftest import BASE_TIME, make_sample

#: An unconfirmed topology is the live-install cause.  It is a *correct*
#: rejection: the association is unverified, so the sample must not train.
CANDIDATE = "candidate"
CONFIRMED = "confirmed"


def _accounting(*, accepted: int = 0, rejected: dict[str, int] | None = None) -> LearningAccounting:
    counter = LearningAccounting()
    for _ in range(accepted):
        counter.record(True)
    for reason, count in (rejected or {}).items():
        for _ in range(count):
            counter.record(False, reason)
    return counter


# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #


def test_counters_are_monotonic_and_add_up() -> None:
    counter = _accounting(accepted=3, rejected={"ambiguous_topology": 2})
    first = counter.snapshot()
    assert first["samples_seen"] == 5
    assert first["samples_accepted"] == 3
    assert first["samples_rejected"] == 2
    assert first["samples_seen"] == first["samples_accepted"] + first["samples_rejected"]

    counter.record(True)
    counter.record(False, "missing_measurement")
    second = counter.snapshot()
    for key in ("samples_seen", "samples_accepted", "samples_rejected"):
        assert second[key] >= first[key], key
    assert second["samples_seen"] == 7


def test_accounting_never_grows_with_traffic() -> None:
    """One hundred thousand rejections must not grow the tally.

    Cost is one integer per label, and the label set is bounded by
    ``MAX_REJECTION_LABELS`` — the same memory whether the engine has seen ten
    samples or a million.
    """
    counter = LearningAccounting()
    for _ in range(100_000):
        counter.record(False, SampleRejection.MISSING_MEASUREMENT.value)
    snapshot = counter.snapshot()
    assert snapshot["samples_rejected"] == 100_000
    assert len(snapshot["rejection_reasons"]) == 1
    assert snapshot["dominant_rejection_reason"] == "missing_measurement"


def test_unknown_reasons_are_bounded_by_other() -> None:
    counter = LearningAccounting(max_labels=3)
    for i in range(50):
        counter.record(False, f"invented_reason_{i}")
    reasons = counter.reasons()
    assert len(reasons) <= 3
    assert OTHER_LABEL in reasons
    assert sum(reasons.values()) == 50


def test_reset_clears_everything() -> None:
    counter = _accounting(accepted=2, rejected={"ambiguous_topology": 4})
    counter.reset()
    assert counter.snapshot()["samples_seen"] == 0
    assert counter.snapshot()["rejection_reasons"] == {}


# --------------------------------------------------------------------------- #
# Dominant reason
# --------------------------------------------------------------------------- #


def test_dominant_reason_from_a_mixed_batch() -> None:
    counter = _accounting(
        accepted=4,
        rejected={
            "missing_measurement": 5,
            "ambiguous_topology": 2,
            "stale_measurement": 2,
        },
    )
    assert counter.dominant() == ("missing_measurement", 5)
    assert counter.snapshot()["dominant_rejection_count"] == 5
    # Ordered most frequent first, so a UI can render the top-N directly.
    assert list(counter.reasons()) == [
        "missing_measurement",
        "ambiguous_topology",
        "stale_measurement",
    ]


def test_dominant_reason_breaks_ties_deterministically() -> None:
    counter = _accounting(rejected={"zeta": 3, "alpha": 3})
    assert counter.dominant() == ("alpha", 3)


def test_no_rejections_means_no_dominant_reason() -> None:
    counter = _accounting(accepted=5)
    assert counter.dominant() == (None, 0)


def test_reasons_accept_enum_values_and_none() -> None:
    counter = LearningAccounting()
    counter.record(False, SampleRejection.AMBIGUOUS_TOPOLOGY)
    counter.record(False, None)
    assert counter.reasons() == {
        SampleRejection.AMBIGUOUS_TOPOLOGY.value: 1,
        "rejected_no_reason": 1,
    }


# --------------------------------------------------------------------------- #
# effective_state
# --------------------------------------------------------------------------- #


def test_blocked_when_every_sample_is_rejected() -> None:
    status = build_learning_status(
        enabled=True,
        active=True,
        samples_seen=28,
        samples_accepted=0,
        samples_rejected=28,
        rejection_reasons={"ambiguous_topology": 28},
        topology_quality=CANDIDATE,
    )
    assert status["effective_state"] == "blocked"
    assert status["dominant_rejection_reason"] == "ambiguous_topology"
    assert status["acceptance_rate"] == 0.0
    assert "ambiguous_topology" in status["summary"]
    assert status["remedy"] == CONFIRM_REMEDY
    assert "confirm_topology" in status["remedy"]


def test_learning_when_samples_are_accepted() -> None:
    status = build_learning_status(
        enabled=True,
        active=True,
        samples_seen=10,
        samples_accepted=7,
        samples_rejected=3,
        rejection_reasons={"missing_measurement": 3},
        topology_quality=CONFIRMED,
    )
    assert status["effective_state"] == "learning"
    assert status["acceptance_rate"] == pytest.approx(0.7)
    assert status["remedy"] is None


def test_blocked_when_almost_all_samples_are_rejected() -> None:
    """A 2% acceptance rate is not "learning"; it is effectively blocked."""
    status = build_learning_status(
        enabled=True,
        active=True,
        samples_seen=ALMOST_ALL_MIN_SAMPLES * 10,
        samples_accepted=2,
        samples_rejected=ALMOST_ALL_MIN_SAMPLES * 10 - 2,
        rejection_reasons={"missing_measurement": ALMOST_ALL_MIN_SAMPLES * 10 - 2},
        topology_quality=CONFIRMED,
    )
    assert status["effective_state"] == "blocked"


def test_a_small_bad_batch_is_not_blocked() -> None:
    """Below the sample floor, a rejection is a bad batch, not a verdict."""
    status = build_learning_status(
        enabled=True,
        active=True,
        samples_seen=4,
        samples_accepted=0,
        samples_rejected=4,
        rejection_reasons={"missing_measurement": 4},
        topology_quality=CONFIRMED,
    )
    # 100% rejected is always blocked regardless of the floor.
    assert status["effective_state"] == "blocked"
    status = build_learning_status(
        enabled=True,
        active=True,
        samples_seen=4,
        samples_accepted=1,
        samples_rejected=3,
        rejection_reasons={"missing_measurement": 3},
        topology_quality=CONFIRMED,
    )
    assert status["effective_state"] == "learning"


def test_blocked_before_the_first_sample_on_a_candidate_topology() -> None:
    """The user learns why on the first poll, not after a week of silence."""
    status = build_learning_status(
        enabled=True,
        active=True,
        samples_seen=0,
        samples_accepted=0,
        samples_rejected=0,
        topology_quality=CANDIDATE,
    )
    assert status["effective_state"] == "blocked"
    assert status["dominant_rejection_reason"] == "ambiguous_topology"
    assert status["blocked_by"] == "unconfirmed_topology"
    assert status["remedy"] == CONFIRM_REMEDY


def test_off_when_the_pipeline_is_disabled() -> None:
    disabled = build_learning_status(enabled=False, active=False)
    assert disabled["effective_state"] == "off"
    assert disabled["samples_seen"] == 0

    paused = build_learning_status(enabled=True, active=False, samples_seen=5, samples_accepted=5)
    assert paused["effective_state"] == "off"


def test_status_is_json_safe() -> None:
    import json

    status = build_learning_status(
        enabled=True,
        active=True,
        samples_seen=2,
        samples_accepted=0,
        samples_rejected=2,
        rejection_reasons={"ambiguous_topology": 2},
        topology_quality=CANDIDATE,
    )
    assert json.loads(json.dumps(status))["effective_state"] == "blocked"


def test_counters_cannot_exceed_seen() -> None:
    """Defensive: a caller-supplied inconsistency must not produce nonsense."""
    status = build_learning_status(
        enabled=True,
        active=True,
        samples_seen=3,
        samples_accepted=99,
        samples_rejected=99,
        topology_quality=CONFIRMED,
    )
    assert status["samples_accepted"] == 3
    assert status["samples_rejected"] == 0


# --------------------------------------------------------------------------- #
# Pipeline wiring: the counting happens where the filter decides
# --------------------------------------------------------------------------- #


@pytest.fixture()
def db(tmp_db_path):
    database = Database(tmp_db_path)
    try:
        yield database
    finally:
        database.close()


def _candidate_sample(index: int):
    """A structurally fine sample whose topology is a candidate."""
    sample = make_sample(
        "dev",
        index,
        baseline=10.0,
        event=110.0,
        state="on",
        captured_at=BASE_TIME,
    )
    return sample.model_copy(update={"notes": [*sample.notes, f"topology_quality:{CANDIDATE}"]})


def test_pipeline_counts_rejected_samples_with_their_reason(db: Database) -> None:
    pipeline = LearningPipeline(db)
    accepted, rejected = pipeline.ingest_samples([_candidate_sample(0)])
    assert accepted == []
    assert len(rejected) == 1
    snapshot = pipeline.accounting.snapshot()
    assert snapshot["samples_seen"] == 1
    assert snapshot["samples_rejected"] == 1
    assert snapshot["dominant_rejection_reason"] == "ambiguous_topology"


def test_pipeline_counts_accepted_samples(db: Database) -> None:
    pipeline = LearningPipeline(db)
    sample = make_sample("dev", 0, baseline=10.0, event=110.0, state="on", captured_at=BASE_TIME)
    accepted, rejected = pipeline.ingest_samples([sample])
    assert len(accepted) == 1 and rejected == []
    assert pipeline.accounting.snapshot()["samples_accepted"] == 1


def test_rejection_logic_is_unchanged_by_the_accounting() -> None:
    """The same unconfirmed-topology sample is still rejected for the same reason.

    This is the guard against "fix the reporting by loosening the filter": an
    unconfirmed topology must keep rejecting samples, because the association
    itself is unverified.  Only the *reporting* of that fact changed.
    """
    from wattmosaic_engine.learning import SampleEvidence

    good = make_sample("dev", 0, baseline=10.0, event=110.0, state="on", captured_at=BASE_TIME)
    filter_ = SampleFilter()
    accepted = filter_.evaluate(good, SampleEvidence(now=datetime(2026, 1, 1, 13, tzinfo=UTC)))
    assert accepted.accepted is True

    # A fresh filter: ``duplicate_state`` is stateful and would otherwise fire
    # first on a second look at the same device/state.
    unconfirmed = good.model_copy(
        update={"notes": [*good.notes, "topology_quality:candidate"]}
    )
    decision = SampleFilter().evaluate(
        unconfirmed, SampleEvidence(now=datetime(2026, 1, 1, 13, tzinfo=UTC))
    )
    assert decision.accepted is False
    assert decision.reason == SampleRejection.AMBIGUOUS_TOPOLOGY


def test_max_label_constant_is_at_least_the_contract_size() -> None:
    assert len(list(SampleRejection)) <= MAX_REJECTION_LABELS


# --------------------------------------------------------------------------- #
# Backend wiring: the counters the health endpoint and HA entity read
# --------------------------------------------------------------------------- #


class _Window:
    """Minimal window accepted by the backend's ``coerce_window``."""

    def __init__(self, readings: dict[str, float], at: datetime) -> None:
        from wattmosaic_engine.electrical.window import ChannelReading

        self.at = at
        self.readings = {
            key: ChannelReading(channel_id=key, value_w=value, age_s=1.0)
            for key, value in readings.items()
        }
        self.cadence_s = 15.0
        self.previous_states = {}
        self.state_transitions = []
        self.metadata = {}


def _topology(*, confirmed: bool):
    from wattmosaic_engine.contracts import (
        CTChannel,
        CTType,
        DeviceTopology,
        Phase,
        Topology,
        TopologyStatus,
    )

    status = TopologyStatus.CONFIRMED if confirmed else TopologyStatus.CANDIDATE
    return Topology(
        status=status,
        channels=[
            CTChannel(ct_id="m_l1", entity_id="sensor.m_l1", ct_type=CTType.MAIN, phase=Phase.L1),
            CTChannel(
                ct_id="ded_fridge",
                entity_id="sensor.fridge",
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                parent_ct_id="m_l1",
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="dev_fridge",
                name="Fridge",
                dedicated_ct_ids=["ded_fridge"],
                topology_status=status,
            ),
        ],
    )


def _feed(backend: Any, topology: Any, *, confirmed: bool) -> None:
    from datetime import timedelta

    start = BASE_TIME
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


def test_backend_reports_blocked_on_an_unconfirmed_topology(db: Database) -> None:
    """The exact live state: loop active, every sample rejected, nothing learned."""
    from wattmosaic_engine.integration import WattMosaicBackend

    backend = WattMosaicBackend(db, learning_enabled=True)
    _feed(backend, _topology(confirmed=False), confirmed=False)

    status = backend.learning_status()
    assert backend.learning_active is True, "the loop itself is running"
    assert status["effective_state"] == "blocked"
    assert status["dominant_rejection_reason"] == "ambiguous_topology"
    assert status["samples_seen"] > 0
    assert status["samples_accepted"] == 0
    assert status["remedy"] == CONFIRM_REMEDY
    assert backend.models_promoted == 0
    assert backend.status()["learning_effective_state"] == "blocked"


def test_backend_reports_learning_on_a_confirmed_topology(db: Database) -> None:
    from wattmosaic_engine.integration import WattMosaicBackend

    backend = WattMosaicBackend(db, learning_enabled=True)
    _feed(backend, _topology(confirmed=True), confirmed=True)

    status = backend.learning_status()
    assert status["effective_state"] == "learning"
    assert status["samples_accepted"] > 0
    assert status["remedy"] is None


def test_backend_counters_are_monotonic_across_cycles(db: Database) -> None:
    from wattmosaic_engine.integration import WattMosaicBackend

    backend = WattMosaicBackend(db, learning_enabled=True)
    topology = _topology(confirmed=False)
    _feed(backend, topology, confirmed=False)
    first = (backend.samples_collected, backend.samples_accepted, backend.samples_rejected)
    _feed(backend, topology, confirmed=False)
    second = (backend.samples_collected, backend.samples_accepted, backend.samples_rejected)
    assert all(b >= a for a, b in zip(first, second, strict=True))
    assert first[0] == first[1] + first[2]


def test_backend_reports_off_when_learning_is_disabled(db: Database) -> None:
    from wattmosaic_engine.integration import WattMosaicBackend

    backend = WattMosaicBackend(db, learning_enabled=False)
    status = backend.learning_status()
    assert status["effective_state"] == "off"
    assert status["samples_seen"] == 0
    assert backend.samples_collected == 0


def test_backend_reports_off_when_paused(db: Database) -> None:
    from wattmosaic_engine.integration import WattMosaicBackend

    backend = WattMosaicBackend(db, learning_enabled=True)
    backend.set_learning_active(False)
    assert backend.learning_status()["effective_state"] == "off"

