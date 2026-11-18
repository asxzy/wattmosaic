"""P1 — canonical ingestion: ReaderMeasurement / HAEvent normalization.

The DEF-01 test is written against the *production* HASource path: a declared
``polarity=inverted`` CT must yield a negative ``active_power_w`` when the raw
HA value is positive, from a ``state_changed`` message to the
:class:`ReaderMeasurement`.  On the unmodified tree HASource never emits a
``ReaderMeasurement`` at all, so this module fails there (DEF-01 confirmed).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    EndpointConfigRevision,
    HaEntityLink,
    HAEvent,
    MeasurementBasis,
    MeasurementType,
    QualityFlag,
    ReaderMeasurement,
)
from wattmosaic_engine.ingestion.ha_source import HASource
from wattmosaic_engine.realtime.candidate_ring import CandidateRing
from wattmosaic_engine.realtime.normalize import (
    REASON_IMPLAUSIBLE_RANGE,
    REASON_NO_REVISION,
    REASON_UNIT_INVALID,
    ReadingNormalizer,
    compatible,
    normalize_reading,
)

T0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
RECEIVED = T0 + timedelta(seconds=2)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def revision(
    *,
    endpoint_id: str = "ct_a",
    rev: int = 1,
    valid_from: datetime = T0 - timedelta(days=1),
    valid_until: datetime | None = None,
    phase: str = "l1",
    leg: str | None = None,
    polarity: str = "normal",
    multiplier: float = 1.0,
    calibration: str = "cal-1",
    measurement_type: str = "instantaneous",
    window_ms: int | None = None,
    declared_by: str = "user",
) -> EndpointConfigRevision:
    return EndpointConfigRevision(
        endpoint_id=endpoint_id,
        revision=rev,
        valid_from=valid_from,
        valid_until=valid_until,
        phase=phase,  # type: ignore[arg-type]
        leg=leg,  # type: ignore[arg-type]
        voltage_reference_v=230.0 if phase in ("l1", "l2", "both") else None,
        polarity=polarity,  # type: ignore[arg-type]
        multiplier=multiplier,
        calibration_version=calibration,
        measurement_type=measurement_type,  # type: ignore[arg-type]
        measurement_window_ms=window_ms,
        expected_interval_s=15.0,
        resolution_w=0.5,
        declared_by=declared_by,  # type: ignore[arg-type]
    )


class FakeRevisionLookup:
    """In-memory stand-in for S13's ``EntitiesRepository.endpoint_revision_at``."""

    def __init__(self, revisions: list[EndpointConfigRevision]) -> None:
        self.revisions = revisions

    @staticmethod
    def _canonical(endpoint_id: str) -> str:
        """HASource derives reader ids from entity ids: ``sensor.ct_a_power``→``ct_a``."""
        base = endpoint_id.split(".", 1)[-1]
        for suffix in ("_power", "_energy"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        return base

    def endpoint_revision_at(
        self, endpoint_id: str, at: datetime | str
    ) -> EndpointConfigRevision | None:
        wanted = self._canonical(endpoint_id)
        matches = [
            r
            for r in self.revisions
            if self._canonical(r.endpoint_id) == wanted
            and r.valid_from <= at
            and (r.valid_until is None or r.valid_until > at)
        ]
        return max(matches, key=lambda r: r.revision) if matches else None


class FakeLinkLookup:
    """In-memory stand-in for S13's ``EntitiesRepository.links_for_entity``."""

    def __init__(self, links: list[HaEntityLink] | None = None) -> None:
        self.links = links or []

    def links_for_entity(
        self, entity_id: str, at: datetime | str | None = None
    ) -> list[HaEntityLink]:
        return [
            link
            for link in self.links
            if link.entity_id == entity_id
            and link.valid_from <= at  # type: ignore[operator]
            and (link.valid_until is None or link.valid_until > at)  # type: ignore[operator]
        ]


def reading(
    *,
    reader_id: str = "ct_a",
    level: float = 100.0,
    phase: str = "l1",
    leg: str | None = None,
    measurement_type: str = "instantaneous",
    current: float | None = None,
    voltage_ref: float | None = None,
) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader_id,
        observed_at=T0,
        received_at=RECEIVED,
        active_power_w=level,
        current_rms_a=current,
        voltage_reference_v=voltage_ref,
        phase=phase,  # type: ignore[arg-type]
        leg=leg,
        measurement_type=MeasurementType(measurement_type),
        calibration_version="cal-1",
        measurement_basis=MeasurementBasis.CT_MEASURED.value,
        quality_flags=[],
        sequence=1,
    )


def _state_change(
    entity_id: str,
    state: str,
    *,
    unit: str = "W",
    context: dict[str, Any] | None = None,
    initial: bool = False,
) -> dict[str, Any]:
    new_state: dict[str, Any] = {
        "entity_id": entity_id,
        "state": state,
        "last_updated": T0.isoformat(),
        "last_changed": T0.isoformat(),
        "attributes": {"unit_of_measurement": unit, "device_class": "power"},
    }
    if context is not None:
        new_state["context"] = context
    return {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "time_fired": T0.isoformat(),
            "data": {
                "entity_id": entity_id,
                "old_state": {"entity_id": entity_id, "state": "0"},
                "new_state": new_state,
            },
        },
    }


def _source(
    revisions: list[EndpointConfigRevision] | None,
    *,
    links: list[HaEntityLink] | None = None,
) -> tuple[HASource, list[ReaderMeasurement], list[HAEvent]]:
    readings: list[ReaderMeasurement] = []
    events: list[HAEvent] = []
    source = HASource(
        "http://ha.local:8123",
        "token",
        revision_lookup=FakeRevisionLookup(revisions or []),
        link_lookup=FakeLinkLookup(links),
        clock=lambda: RECEIVED,
    )
    source._on_reading = readings.append  # type: ignore[assignment]
    source._on_ha_event = events.append  # type: ignore[assignment]
    return source, readings, events


# --------------------------------------------------------------------------- #
# DEF-01 — polarity applied once, end to end
# --------------------------------------------------------------------------- #


async def test_inverted_ct_yields_negative_power_end_to_end() -> None:
    """DEF-01: a declared inverted CT flips a positive raw HA value."""
    inverted = revision(endpoint_id="ct_a", polarity="inverted", multiplier=1.0)
    source, readings, _ = _source([inverted])

    await source._dispatch(_state_change("sensor.ct_a_power", "100"))

    assert len(readings) == 1, "HASource must emit a canonical ReaderMeasurement"
    assert readings[0].active_power_w == -100.0


def test_inverted_polarity_pure_normalizer() -> None:
    result = normalize_reading(
        reader_id="ct_a",
        raw_value="100",
        unit="W",
        observed_at=T0,
        received_at=RECEIVED,
        revision=revision(polarity="inverted", multiplier=2.0),
        sequence=1,
    )
    # polarity first (-100), then multiplier (-200), exactly once.
    assert result.measurement.active_power_w == -200.0
    assert result.quarantined is False


# --------------------------------------------------------------------------- #
# Revision resolution / DEF-06
# --------------------------------------------------------------------------- #


def test_multiplier_revision_applies_only_from_valid_from() -> None:
    change_at = T0 + timedelta(minutes=10)
    old = revision(rev=1, multiplier=2.0, calibration="cal-old")
    new = revision(
        rev=2,
        multiplier=3.0,
        calibration="cal-new",
        valid_from=change_at,
    )
    normalizer = ReadingNormalizer(FakeRevisionLookup([old, new]))

    before = normalizer.normalize(
        "ct_a",
        raw_value="10",
        unit="W",
        observed_at=T0,
        received_at=RECEIVED,
        sequence=1,
    )
    after = normalizer.normalize(
        "ct_a",
        raw_value="10",
        unit="W",
        observed_at=change_at + timedelta(seconds=1),
        received_at=RECEIVED,
        sequence=2,
    )

    assert before.measurement.active_power_w == 20.0
    assert before.measurement.calibration_version == "cal-old"
    assert after.measurement.active_power_w == 30.0
    assert after.measurement.calibration_version == "cal-new"


def test_revision_stamps_measurement_metadata() -> None:
    result = normalize_reading(
        reader_id="ct_a",
        raw_value="12",
        unit="W",
        observed_at=T0,
        received_at=RECEIVED,
        revision=revision(
            phase="l2",
            leg="l2",
            calibration="cal-9",
            measurement_type="window_average",
            window_ms=15000,
        ),
        sequence=7,
    )
    m = result.measurement
    assert m.phase == "l2"
    assert m.leg == "l2"
    assert m.calibration_version == "cal-9"
    assert m.measurement_type == MeasurementType.WINDOW_AVERAGE.value
    assert m.measurement_window_ms == 15000


def test_unknown_phase_stays_unknown() -> None:
    result = normalize_reading(
        reader_id="ct_a",
        raw_value="5",
        unit="W",
        observed_at=T0,
        received_at=RECEIVED,
        revision=revision(phase="unknown", leg=None, polarity="normal"),
        sequence=1,
    )
    assert result.measurement.phase == "unknown"
    assert result.measurement.leg is None


def test_missing_revision_is_quarantined() -> None:
    normalizer = ReadingNormalizer(FakeRevisionLookup([]))
    result = normalizer.normalize(
        "ct_unknown",
        raw_value="10",
        unit="W",
        observed_at=T0,
        received_at=RECEIVED,
        sequence=1,
    )
    assert result.quarantined is True
    assert result.quarantine_reason == REASON_NO_REVISION
    assert result.measurement.active_power_w is None
    assert normalizer.quarantine.counts_for("ct_unknown")[REASON_NO_REVISION] == 1


# --------------------------------------------------------------------------- #
# Quarantine
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "unavailable", ""])
def test_non_finite_and_unparseable_values_quarantined(raw: str) -> None:
    result = normalize_reading(
        reader_id="ct_a",
        raw_value=raw,
        unit="W",
        observed_at=T0,
        received_at=RECEIVED,
        revision=revision(),
        sequence=1,
    )
    assert result.quarantined is True
    assert result.quarantine_reason == REASON_UNIT_INVALID
    assert result.measurement.active_power_w is None
    assert QualityFlag.UNIT_INVALID in result.measurement.quality_flags


def test_unsupported_unit_quarantined() -> None:
    result = normalize_reading(
        reader_id="ct_a",
        raw_value="10",
        unit="furlongs",
        observed_at=T0,
        received_at=RECEIVED,
        revision=revision(),
        sequence=1,
    )
    assert result.quarantined is True
    assert result.quarantine_reason == REASON_UNIT_INVALID


def test_implausible_range_quarantined_not_delivered() -> None:
    result = normalize_reading(
        reader_id="ct_a",
        raw_value="30000",
        unit="W",
        observed_at=T0,
        received_at=RECEIVED,
        revision=revision(),
        sequence=1,
        reader_max_w=24000.0,
    )
    assert result.quarantined is True
    assert result.quarantine_reason == REASON_IMPLAUSIBLE_RANGE
    assert result.measurement.active_power_w is None
    assert QualityFlag.IMPLAUSIBLE_RANGE in result.measurement.quality_flags


def test_quarantine_counts_per_reader_and_reason() -> None:
    normalizer = ReadingNormalizer(FakeRevisionLookup([revision()]), reader_max_w=24000.0)
    for _ in range(2):
        normalizer.normalize(
            "ct_a", raw_value="nan", unit="W",
            observed_at=T0, received_at=RECEIVED, sequence=1,
        )
    normalizer.normalize(
        "ct_a", raw_value="30000", unit="W",
        observed_at=T0, received_at=RECEIVED, sequence=2,
    )
    normalizer_b = ReadingNormalizer(
        FakeRevisionLookup([revision(endpoint_id="ct_b")]), reader_max_w=24000.0
    )
    normalizer_b.quarantine = normalizer.quarantine
    normalizer_b.normalize(
        "ct_b", raw_value="nan", unit="W",
        observed_at=T0, received_at=RECEIVED, sequence=3,
    )
    assert normalizer.quarantine.counts_for("ct_a") == Counter(
        {REASON_UNIT_INVALID: 2, REASON_IMPLAUSIBLE_RANGE: 1}
    )
    assert normalizer.quarantine.counts_for("ct_b") == Counter({REASON_UNIT_INVALID: 1})


def test_primed_flag_is_kept_on_delivered_seed() -> None:
    result = normalize_reading(
        reader_id="ct_a",
        raw_value="10",
        unit="W",
        observed_at=T0,
        received_at=RECEIVED,
        revision=revision(),
        sequence=1,
        flags=[QualityFlag.PRIMED],
    )
    assert result.quarantined is False
    assert QualityFlag.PRIMED in result.measurement.quality_flags


async def test_prime_replay_is_flagged_primed() -> None:
    source, readings, _ = _source([revision()])
    await source._handle_state(
        {"entity_id": "sensor.ct_a_power", "state": "10",
         "last_updated": T0.isoformat(),
         "attributes": {"unit_of_measurement": "W", "device_class": "power"}},
        initial=True,
    )
    assert len(readings) == 1
    assert QualityFlag.PRIMED in readings[0].quality_flags


# --------------------------------------------------------------------------- #
# HAEvent context + linked
# --------------------------------------------------------------------------- #


async def test_context_captured_and_linked_from_ha_entity_links() -> None:
    link = HaEntityLink(
        entity_id="switch.lamp",
        target_kind="device",
        target_id="dev_lamp",
        role="load_state",  # type: ignore[arg-type]
        valid_from=T0 - timedelta(days=1),
        declared_by="migration",
    )
    source, _, events = _source([], links=[link])
    await source._dispatch(
        _state_change(
            "switch.lamp",
            "on",
            context={"id": "ctx-1", "parent_id": "ctx-parent", "user_id": "user-1"},
        )
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.context_id == "ctx-1"
    assert ev.context_parent_id == "ctx-parent"
    assert ev.context_user_id == "user-1"
    assert ev.linked is True


async def test_unlinked_event_is_not_linked() -> None:
    source, _, events = _source([])
    await source._dispatch(_state_change("switch.unknown", "on", context={"id": "ctx-2"}))
    assert len(events) == 1
    assert events[0].linked is False
    assert events[0].context_id == "ctx-2"


async def test_unlinked_events_are_held_in_candidate_ring() -> None:
    ring = CandidateRing()
    source = HASource(
        "http://ha.local:8123",
        "token",
        link_lookup=FakeLinkLookup([]),
        clock=lambda: RECEIVED,
        candidate_ring=ring,
    )
    await source._dispatch(_state_change("switch.lamp", "on", context={"id": "ctx-r"}))  # type: ignore[arg-type]
    assert len(ring) == 1
    assert ring.by_context("ctx-r")[0].entity_id == "switch.lamp"

    # Linked events are not held.
    from wattmosaic_engine.contracts import HaEntityLink

    link = HaEntityLink(
        entity_id="switch.lamp",
        target_kind="device",
        target_id="dev_lamp",
        role="load_state",  # type: ignore[arg-type]
        valid_from=T0 - timedelta(days=1),
        declared_by="migration",
    )
    source._link_lookup = FakeLinkLookup([link])
    await source._dispatch(_state_change("switch.lamp", "off", context={"id": "ctx-r2"}))  # type: ignore[arg-type]
    assert len(ring) == 1


# --------------------------------------------------------------------------- #
# compatible() truth table (design §2.6)
# --------------------------------------------------------------------------- #


def test_compatible_truth_table() -> None:
    a = reading()

    ok, reason = compatible(a, reading())
    assert ok is True and reason is None

    ok, reason = compatible(a, reading(measurement_type="accumulated_energy"))
    assert ok is False and reason == "measurement_type_family_mismatch"

    no_power = reading()
    no_power.active_power_w = None
    ok, reason = compatible(a, no_power)
    assert ok is False and reason == "missing_active_power"

    ok, reason = compatible(reading(phase="l1"), reading(phase="l2"))
    assert ok is False and reason == "phase_mismatch"

    ok, reason = compatible(reading(phase="both"), reading(phase="l1"))
    assert ok is True and reason is None

    ok, reason = compatible(
        reading(reader_id="ct_a", phase="l1", leg="l1"),
        reading(reader_id="ct_b", phase="l2", leg="l2"),
    )
    assert ok is False  # not a declared paired leg

    ok, reason = compatible(
        reading(reader_id="ct_a", phase="l1", leg="l1"),
        reading(reader_id="ct_b", phase="l2", leg="l2"),
        paired_legs={frozenset({"ct_a", "ct_b"})},
    )
    assert ok is True and reason is None

    ok, reason = compatible(
        reading(current=3.0, voltage_ref=None),
        reading(current=None, voltage_ref=None),
    )
    assert ok is False and reason == "current_without_voltage_reference"

    ok, reason = compatible(
        reading(current=3.0, voltage_ref=230.0),
        reading(current=None, voltage_ref=230.0),
    )
    assert ok is True and reason is None
