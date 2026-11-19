"""Canonical measurement normalization at ingestion (P1, design §2).

Rules, in order:

1. Unit canon + finite check (reuse :mod:`ingestion.normalize`).  Bad values are
   *quarantined*: counted, flagged, never delivered to reader state.
2. Resolve the :class:`EndpointConfigRevision` valid at ``observed_at`` (S13
   repository or an in-memory fake with the same ``endpoint_revision_at`` shape).
   Apply polarity, then the multiplier, **exactly once**; stamp
   ``calibration_version``, ``measurement_type``, ``measurement_window_ms``,
   phase and leg on the resulting :class:`ReaderMeasurement` (DEF-01, DEF-06).
3. Plausibility: ``|P| <= reader_max_w`` (declared, or 24 kW default).  Beyond
   that → ``IMPLAUSIBLE_RANGE``, quarantined.
4. Compatibility predicate ``compatible(a, b) -> (bool, reason)`` per design
   §2.6: same measurement-type family, ``active_power_w`` on both, compatible
   phase (equal / either ``both`` / a declared paired leg), and current (A)
   only with a voltage reference on both.

Pure where the task says so: all time comes from explicit arguments, no DB and
no wall clock.  Absent values stay ``None`` (HB §18); there is no ``or 0.0``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from ..contracts import (
    EndpointConfigRevision,
    MeasurementBasis,
    MeasurementType,
    QualityFlag,
    ReaderMeasurement,
)
from ..ingestion.normalize import canonical_unit, normalize_unit, parse_float


class QuarantineReason(str, Enum):
    """Why a value was kept out of reader state (design §2.1-2.3)."""

    UNIT_INVALID = "unit_invalid"
    IMPLAUSIBLE_RANGE = "implausible_range"
    NO_REVISION = "no_revision"


# Stable string aliases used by the tests and diagnostics.
REASON_UNIT_INVALID = QuarantineReason.UNIT_INVALID.value
REASON_IMPLAUSIBLE_RANGE = QuarantineReason.IMPLAUSIBLE_RANGE.value
REASON_NO_REVISION = QuarantineReason.NO_REVISION.value

#: Default ceiling when an endpoint revision does not declare one (24 kW).
DEFAULT_READER_MAX_W = 24000.0

_ENERGY_TYPES = {MeasurementType.ACCUMULATED_ENERGY.value}


@dataclass
class QuarantineCounter:
    """Per-reader, per-reason counts of quarantined values (diagnostics)."""

    _counts: dict[str, Counter[str]] = field(default_factory=dict)

    def record(self, reader_id: str, reason: QuarantineReason | str) -> None:
        value = reason.value if isinstance(reason, QuarantineReason) else str(reason)
        self._counts.setdefault(reader_id, Counter())[value] += 1

    def counts_for(self, reader_id: str) -> Counter[str]:
        """A copy of the counts for one reader; readers with none yield empty."""
        return Counter(self._counts.get(reader_id, ()))

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {reader: dict(counts) for reader, counts in self._counts.items()}


@dataclass(frozen=True)
class NormalizeResult:
    """The canonical reading plus its delivery/quarantine disposition."""

    measurement: ReaderMeasurement
    quarantined: bool
    quarantine_reason: str | None = None

    @property
    def deliverable(self) -> bool:
        return not self.quarantined


class ReadingNormalizer:
    """Resolve revisions and normalize raw HA values into canonical readings.

    ``revision_lookup`` is any object exposing
    ``endpoint_revision_at(endpoint_id, at) -> EndpointConfigRevision | None``
    (S13's repository and the in-memory fake share this shape).  The class never
    imports a repository or a fake: production code depends only on that
    protocol-like shape, and the tests supply deterministic doubles.
    """

    def __init__(
        self,
        revision_lookup: Any,
        *,
        reader_max_w: float = DEFAULT_READER_MAX_W,
        quarantine: QuarantineCounter | None = None,
    ) -> None:
        self._revision_lookup = revision_lookup
        self.reader_max_w = float(reader_max_w)
        self.quarantine = quarantine if quarantine is not None else QuarantineCounter()

    def normalize(
        self,
        reader_id: str,
        *,
        raw_value: Any,
        unit: str | None,
        observed_at: datetime,
        received_at: datetime,
        sequence: int,
        flags: list[QualityFlag] | None = None,
        measurement_basis: str = MeasurementBasis.CT_MEASURED.value,
        current_rms_a: float | None = None,
        voltage_rms_v: float | None = None,
        reactive_power_var: float | None = None,
        power_factor: float | None = None,
        energy_wh: float | None = None,
    ) -> NormalizeResult:
        """Normalize one raw value into a :class:`NormalizeResult`.

        The returned measurement always exists (so a caller may log it), but its
        ``active_power_w`` is ``None`` and its quality flags record the reason
        when the value was quarantined.
        """
        quality_flags: list[QualityFlag] = list(flags or ())

        revision = self._revision_lookup.endpoint_revision_at(reader_id, observed_at)
        if revision is None:
            return self._quarantine(
                reader_id, observed_at, received_at, sequence, quality_flags,
                QuarantineReason.NO_REVISION, measurement_basis,
                raw_value=raw_value, unit=unit,
            )

        # 1. Unit canon + finite check.  Energy stays energy (no watts).
        numeric = parse_float(raw_value)
        canon = canonical_unit(unit)
        if numeric is None or canon is None:
            return self._quarantine(
                reader_id, observed_at, received_at, sequence, quality_flags,
                QuarantineReason.UNIT_INVALID, measurement_basis,
                revision=revision, raw_value=raw_value, unit=unit,
            )
        try:
            value, base_unit = normalize_unit(numeric, canon)
        except ValueError:
            return self._quarantine(
                reader_id, observed_at, received_at, sequence, quality_flags,
                QuarantineReason.UNIT_INVALID, measurement_basis,
                revision=revision, raw_value=raw_value, unit=unit,
            )

        measurement_type = _measurement_type(revision.measurement_type)
        active_power: float | None
        energy: float | None = energy_wh
        if base_unit == "kWh":
            # Accumulated energy: never fabricate a watt reading (HB §2.1).
            active_power = None
            energy = value * 1000.0 if canon == "kWh" else value
            measurement_type = MeasurementType.ACCUMULATED_ENERGY
        else:
            # 2. Polarity first, then multiplier, exactly once.
            signed = -value if revision.polarity == "inverted" else value
            active_power = signed * float(revision.multiplier)

        # 3. Plausibility on the signed, scaled power (energy is exempt).
        if active_power is not None and abs(active_power) > self.reader_max_w:
            return self._quarantine(
                reader_id, observed_at, received_at, sequence, quality_flags,
                QuarantineReason.IMPLAUSIBLE_RANGE, measurement_basis,
                revision=revision, raw_value=raw_value, unit=unit,
                active_power_w=active_power, energy_wh=energy,
            )

        measurement = self._build(
            reader_id, observed_at, received_at, sequence, quality_flags,
            revision, measurement_type, measurement_basis,
            active_power_w=active_power,
            current_rms_a=current_rms_a,
            voltage_rms_v=voltage_rms_v,
            reactive_power_var=reactive_power_var,
            power_factor=power_factor,
            energy_wh=energy,
        )
        return NormalizeResult(measurement=measurement, quarantined=False)

    # -- internals --------------------------------------------------------- #

    def _quarantine(
        self,
        reader_id: str,
        observed_at: datetime,
        received_at: datetime,
        sequence: int,
        quality_flags: list[QualityFlag],
        reason: QuarantineReason,
        measurement_basis: str,
        *,
        revision: EndpointConfigRevision | None = None,
        raw_value: Any = None,
        unit: str | None = None,
        active_power_w: float | None = None,
        energy_wh: float | None = None,
    ) -> NormalizeResult:
        self.quarantine.record(reader_id, reason)
        flag = {
            QuarantineReason.UNIT_INVALID: QualityFlag.UNIT_INVALID,
            QuarantineReason.IMPLAUSIBLE_RANGE: QualityFlag.IMPLAUSIBLE_RANGE,
            QuarantineReason.NO_REVISION: QualityFlag.UNIT_INVALID,
        }[reason]
        if flag not in quality_flags:
            quality_flags = [*quality_flags, flag]
        if revision is None:
            measurement = ReaderMeasurement(
                reader_id=reader_id,
                observed_at=observed_at,
                received_at=received_at,
                active_power_w=None,
                phase="unknown",
                leg=None,
                measurement_type=MeasurementType.INSTANTANEOUS,
                calibration_version="unknown",
                measurement_basis=measurement_basis,
                quality_flags=quality_flags,
                sequence=sequence,
            )
        else:
            # A quarantined reading is never delivered: it carries no value,
            # whatever the reason.  The reason lives in the quality flags.
            measurement = self._build(
                reader_id, observed_at, received_at, sequence, quality_flags,
                revision, _measurement_type(revision.measurement_type), measurement_basis,
                active_power_w=None,
                energy_wh=None,
            )
        return NormalizeResult(
            measurement=measurement, quarantined=True, quarantine_reason=reason.value
        )

    @staticmethod
    def _build(
        reader_id: str,
        observed_at: datetime,
        received_at: datetime,
        sequence: int,
        quality_flags: list[QualityFlag],
        revision: EndpointConfigRevision,
        measurement_type: MeasurementType,
        measurement_basis: str,
        *,
        active_power_w: float | None = None,
        current_rms_a: float | None = None,
        voltage_rms_v: float | None = None,
        reactive_power_var: float | None = None,
        power_factor: float | None = None,
        energy_wh: float | None = None,
    ) -> ReaderMeasurement:
        return ReaderMeasurement(
            reader_id=reader_id,
            observed_at=observed_at,
            received_at=received_at,
            active_power_w=active_power_w,
            current_rms_a=current_rms_a,
            voltage_rms_v=voltage_rms_v,
            reactive_power_var=reactive_power_var,
            power_factor=power_factor,
            energy_wh=energy_wh,
            phase=revision.phase,
            leg=revision.leg,
            voltage_reference_v=revision.voltage_reference_v,
            measurement_type=measurement_type,
            measurement_window_ms=revision.measurement_window_ms,
            calibration_version=revision.calibration_version,
            measurement_basis=measurement_basis,
            quality_flags=quality_flags,
            sequence=sequence,
        )


def normalize_reading(
    *,
    reader_id: str,
    raw_value: Any,
    unit: str | None,
    observed_at: datetime,
    received_at: datetime,
    revision: EndpointConfigRevision,
    sequence: int,
    reader_max_w: float = DEFAULT_READER_MAX_W,
    flags: list[QualityFlag] | None = None,
) -> NormalizeResult:
    """Convenience: normalize against a single, already-resolved revision."""

    class _FixedLookup:
        def endpoint_revision_at(
            self, endpoint_id: str, at: datetime
        ) -> EndpointConfigRevision | None:
            return revision

    return ReadingNormalizer(_FixedLookup(), reader_max_w=reader_max_w).normalize(
        reader_id,
        raw_value=raw_value,
        unit=unit,
        observed_at=observed_at,
        received_at=received_at,
        sequence=sequence,
        flags=flags,
    )


def _measurement_type(value: str) -> MeasurementType:
    try:
        return MeasurementType(value)
    except ValueError:
        return MeasurementType.INSTANTANEOUS


# --------------------------------------------------------------------------- #
# Compatibility predicate (design §2.6)
# --------------------------------------------------------------------------- #

PhaseLiteral = Literal["l1", "l2", "both", "unknown"]


def _family(measurement_type: MeasurementType | str) -> str:
    value = measurement_type.value if hasattr(measurement_type, "value") else str(measurement_type)
    return "energy" if value == MeasurementType.ACCUMULATED_ENERGY.value else "power"


def _has_current(measurement: ReaderMeasurement) -> bool:
    return measurement.current_rms_a is not None


def _has_voltage_reference(measurement: ReaderMeasurement) -> bool:
    return measurement.voltage_reference_v is not None


def compatible(
    a: ReaderMeasurement,
    b: ReaderMeasurement,
    *,
    paired_legs: set[frozenset[str]] | None = None,
) -> tuple[bool, str | None]:
    """Whether two readers may share an equation (design §2.6).

    Returns ``(True, None)`` when compatible, otherwise ``(False, reason)``.
    ``paired_legs`` declares reader-id pairs that form a PAIRED_LEG.
    """
    if _family(a.measurement_type) != _family(b.measurement_type):
        return False, "measurement_type_family_mismatch"
    if a.active_power_w is None or b.active_power_w is None:
        return False, "missing_active_power"

    phases = (a.phase, b.phase)
    if "unknown" in phases:
        return False, "phase_mismatch"
    if a.phase != b.phase and "both" not in phases:
        paired = paired_legs or set()
        if frozenset({a.reader_id, b.reader_id}) not in paired:
            return False, "phase_mismatch"

    if (_has_current(a) or _has_current(b)) and not (
        _has_voltage_reference(a) and _has_voltage_reference(b)
    ):
        return False, "current_without_voltage_reference"

    return True, None


__all__ = [
    "DEFAULT_READER_MAX_W",
    "REASON_IMPLAUSIBLE_RANGE",
    "REASON_NO_REVISION",
    "REASON_UNIT_INVALID",
    "NormalizeResult",
    "QuarantineCounter",
    "QuarantineReason",
    "ReadingNormalizer",
    "compatible",
    "normalize_reading",
]
