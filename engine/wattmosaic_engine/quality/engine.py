"""Data-quality engine: per-channel scoring, thresholds and event logging.

Scoring model
-------------
Every channel starts at ``1.0`` and loses points for each detected defect.  The
channel status is derived from its score using :data:`QUALITY_THRESHOLDS`:

===============  =============  =============================================
score            status         meaning
===============  =============  =============================================
>= 0.80           ``good``      usable for inference and learning
0.55 – 0.80       ``degraded``  usable with reduced confidence; not for learning
0.30 – 0.55       ``stale``     value may be shown, but must not be learned from
< 0.30            ``bad``       unusable; treat as unavailable
no sample         ``missing``   channel has never produced data in the window
===============  =============  =============================================

Power vs energy domains
-----------------------
Power checks (age, noise, resolution, overlap, plausibility, step changes) apply
**only** to power entities (``device_class=power`` / unit ``W`` / ``kW``).
Cumulative energy entities (``kWh`` / ``Wh``) are evaluated in their own domain:

* monotonicity — a cumulative counter must not decrease;
* counter resets — a decrease is reported as ``energy_counter_reset`` with the
  drop in **kWh**, never relabelled as watts;
* staleness and availability, as for power.

Energy values are reported as ``value_kwh`` and never appear as ``value_w``.
They never enter the power matrix.

Overlap handling
----------------
Identical simultaneous non-zero values across many entities are grouped into a
**single** ``measurement_overlap`` issue listing every member and the cluster
size.  The issue carries a warning severity plus an explicit explanation that
identical simultaneous values indicate a template/duplicate source, and that such
channels must not be treated as independent measurements or as direct
measurements.  The channels are still reported; their reliability contribution
is reduced rather than removed.

Thresholds are configurable via :class:`QualityThresholds`.  Every defect is
recorded in the ``issues`` list with the channel name, and optionally persisted to
``data_quality_events`` through ``Database.log_quality_event`` with a per
(channel, code) cooldown so a persistent fault does not flood the database.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..contracts import (
    CTChannel,
    DataQualityReport,
    Phase,
    Polarity,
    QualityStatus,
    Reconciliation,
    Topology,
    utcnow,
)
from ..synchronization.aligner import (
    AlignedChannel,
    AlignedWindow,
    ChannelHealth,
    EnergyHealth,
    EnergyWindow,
)

log = logging.getLogger("wattmosaic.quality")

#: Documented default thresholds (score boundaries for each status).
QUALITY_THRESHOLDS: dict[str, float] = {
    "good_min": 0.80,
    "degraded_min": 0.55,
    "stale_min": 0.30,
}

POWER_UNITS = ("W", "kW")
ENERGY_UNITS = ("kWh", "Wh")

OVERLAP_EXPLANATION = (
    "identical simultaneous values across independent entities indicate a shared "
    "template or duplicate source; they are not independent measurements and must "
    "not be used as direct measurements. Reliability contribution reduced."
)
DUPLICATE_ENTITY_EXPLANATION = (
    "the same Home Assistant entity is bound to more than one channel; the "
    "observations are not independent and must not be double-counted."
)


@dataclass(slots=True)
class QualityThresholds:
    """All tunables for quality evaluation.

    Penalties are additive; scores are clamped to ``[0, 1]`` after every step.
    """

    # Score boundaries
    good_min: float = QUALITY_THRESHOLDS["good_min"]
    degraded_min: float = QUALITY_THRESHOLDS["degraded_min"]
    stale_min: float = QUALITY_THRESHOLDS["stale_min"]

    # Age / staleness
    stale_after_s: float = 120.0
    max_age_factor: float = 2.0  # score decays above expected_interval * factor
    stale_penalty: float = 0.65
    age_penalty: float = 0.20

    # Timestamp consistency (received_at - state_timestamp)
    skew_warn_s: float = 5.0
    skew_bad_s: float = 30.0
    skew_penalty: float = 0.10
    future_skew_penalty: float = 0.25

    # Interval anomalies
    interval_jitter_ratio_warn: float = 3.0
    interval_slow_factor_warn: float = 4.0
    interval_penalty: float = 0.15

    # Repeated / missing
    repeated_penalty: float = 0.20
    missing_penalty: float = 1.0

    # Noise / resolution (power domain only)
    noise_relative_warn: float = 0.20
    noise_floor_w: float = 5.0
    noise_penalty: float = 0.15
    resolution_relative_warn: float = 0.10
    resolution_penalty: float = 0.10

    # Implausible values (power domain only)
    absurd_power_w: float = 200_000.0  # 200 kW on a single residential channel
    implausible_penalty: float = 0.60

    # Energy counter domain
    energy_absurd_kwh: float = 1_000_000.0
    energy_reset_penalty: float = 0.30
    energy_reset_min_drop_kwh: float = 1e-6

    # Topology / polarity / phase
    polarity_penalty: float = 0.10
    phase_unknown_penalty: float = 0.02
    overlap_penalty: float = 0.25
    #: Minimum cluster size before identical values are treated as duplicates.
    overlap_min_cluster: int = 2
    #: Identical values below this magnitude (W) are not considered meaningful.
    overlap_min_abs_w: float = 0.5

    # Reconciliation
    reconciliation_warn_ratio: float = 0.15
    reconciliation_bad_ratio: float = 0.40
    reconciliation_penalty: float = 0.30

    # Event logging
    log_events: bool = True
    log_cooldown_s: float = 300.0
    log_min_severity: str = "warning"  # info | warning | error


def score_to_status(score: float, thresholds: QualityThresholds | None = None) -> QualityStatus:
    t = thresholds or QualityThresholds()
    if score >= t.good_min:
        return QualityStatus.GOOD
    if score >= t.degraded_min:
        return QualityStatus.DEGRADED
    if score >= t.stale_min:
        return QualityStatus.STALE
    return QualityStatus.BAD


def _issue(issues: list[str], channel: str | None, code: str, detail: str) -> str:
    text = f"{channel}: {code} ({detail})" if channel else f"{code} ({detail})"
    issues.append(text)
    return text


def _is_energy_unit(unit: str | None) -> bool:
    return bool(unit) and str(unit) in ENERGY_UNITS


def _is_power_unit(unit: str | None) -> bool:
    return not unit or str(unit) in POWER_UNITS


@dataclass(slots=True)
class _ChannelScore:
    channel: str
    score: float = 1.0
    issues: list[str] = None  # type: ignore[assignment]
    defects: list[tuple[str, str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.issues is None:
            self.issues = []
        if self.defects is None:
            self.defects = []

    def penalize(
        self,
        amount: float,
        code: str,
        detail: str,
        *,
        severity: str = "warning",
        issues: list[str] | None = None,
    ) -> None:
        self.score = max(0.0, self.score - max(0.0, float(amount)))
        self.defects.append((code, severity, detail))
        text = _issue(self.issues, self.channel, code, detail)
        if issues is not None:
            issues.append(text)


def _is_unavailable(
    name: str, entity_id: str | None, cfg: CTChannel | None, unavailable: set[str]
) -> bool:
    """Match unavailable entities by entity id or by the derived channel name."""
    if entity_id and entity_id in unavailable:
        return True
    if cfg is not None and cfg.entity_id in unavailable:
        return True
    if name in unavailable:
        return True
    # The aligner channel name is the entity id without its domain prefix.
    return any(entity.split(".", 1)[-1] == name for entity in unavailable)


@dataclass(frozen=True, slots=True)
class OverlapCluster:
    """A group of channels sharing identical simultaneous values."""

    channels: tuple[str, ...]
    value: float
    timestamp_s: int
    domain: str = "power"

    @property
    def size(self) -> int:
        return len(self.channels)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channels": list(self.channels),
            "size": self.size,
            "value": self.value,
            "unit": "kWh" if self.domain == "energy" else "W",
            "domain": self.domain,
            "timestamp_s": self.timestamp_s,
        }


class DataQualityEngine:
    """Evaluate an :class:`AlignedWindow` and log quality events."""

    def __init__(
        self,
        *,
        database: Any | None = None,
        thresholds: QualityThresholds | None = None,
        stale_after_s: float | None = None,
        expected_intervals: Mapping[str, float] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.db = database
        self.thresholds = thresholds or QualityThresholds()
        if stale_after_s is not None:
            self.thresholds.stale_after_s = float(stale_after_s)
        self.expected_intervals = dict(expected_intervals or {})
        self.log = logger or log
        self._last_logged: dict[tuple[str, str], datetime] = {}
        self._last_report: DataQualityReport | None = None
        self._last_issue_set: set[str] = set()

    # -- topology helpers -------------------------------------------------- #
    def set_expected_intervals(self, mapping: Mapping[str, float]) -> None:
        self.expected_intervals.update({k: float(v) for k, v in mapping.items() if v})

    def _expected_for(self, channel: str, health: ChannelHealth | None) -> float | None:
        if channel in self.expected_intervals:
            return self.expected_intervals[channel]
        if health is not None and health.interval_median_s and health.interval_median_s > 0:
            return health.interval_median_s
        return None

    # -- evaluation -------------------------------------------------------- #
    def evaluate(
        self,
        window: AlignedWindow,
        health: Mapping[str, ChannelHealth] | None = None,
        *,
        topology: Topology | None = None,
        reconciliation: Reconciliation | None = None,
        unavailable: Iterable[str] | None = None,
        energy_window: EnergyWindow | None = None,
        energy_health: Mapping[str, EnergyHealth] | None = None,
        log_events: bool | None = None,
    ) -> DataQualityReport:
        """Score every channel in ``window`` (power) and ``energy_window``.

        ``health`` provides observed intervals/noise; ``topology`` adds
        polarity/phase checks; ``reconciliation`` adds a home-level consistency
        penalty; ``unavailable`` lists entities HA reports as unavailable.
        ``energy_window``/``energy_health`` cover cumulative energy counters and
        are never mixed with power scoring.
        """
        health = health or {}
        energy_health = energy_health or {}
        unavailable_set = set(unavailable or ())
        channel_config = topology.channel_map() if topology is not None else {}
        issues: list[str] = []
        channels: dict[str, dict[str, Any]] = {}
        scores: list[float] = []
        defects_to_log: list[tuple[str, str, str, str]] = []

        power_channels = {
            name: ch for name, ch in window.channels.items() if not ch.is_energy
        }
        # Defensive: a channel in the power window carrying an energy unit is
        # routed to the energy scoring path instead of being scored as watts.
        energy_channels: dict[str, AlignedChannel] = {
            name: ch for name, ch in window.channels.items() if ch.is_energy
        }

        overlap_map, clusters, overlap_issues, overlap_logged = self._power_overlap_issues(
            power_channels, channel_config
        )
        issues.extend(overlap_issues)
        defects_to_log.extend(overlap_logged)

        for name in sorted(power_channels):
            ch = power_channels[name]
            ch_health = health.get(name) or health.get(ch.entity_id or "")
            cfg = channel_config.get(name)
            result = self._score_power_channel(
                name, ch, ch_health, cfg, unavailable_set, issues, overlap_map.get(name, ())
            )
            status = (
                QualityStatus.MISSING
                if ch.value_w is None
                else score_to_status(result.score, self.thresholds)
            )
            channels[name] = {
                "domain": "power",
                "unit": "W",
                "score": round(result.score, 4),
                "status": status.value,
                "existing_quality": ch.quality,
                "value_w": ch.value_w,
                "value_kwh": None,
                "reliability_factor": round(result.score, 4),
                "age_seconds": ch.age_seconds,
                "repeated": ch.repeated,
                "state_timestamp": ch.state_timestamp.isoformat() if ch.state_timestamp else None,
                "received_at": ch.received_at.isoformat() if ch.received_at else None,
                "sample_count": ch_health.sample_count if ch_health else 0,
                "repeat_ratio": ch_health.repeat_ratio if ch_health else 0.0,
                "interval_median_s": ch_health.interval_median_s if ch_health else None,
                "interval_p95_s": ch_health.interval_p95_s if ch_health else None,
                "noise_mad_w": ch_health.noise_mad_w if ch_health else None,
                "resolution_w": ch_health.resolution_w if ch_health else None,
                "fresh": ch.fresh,
                "issues": list(result.issues),
            }
            if ch.value_w is not None:
                scores.append(result.score)
            defects_to_log.extend(
                (name, code, detail, sev) for code, sev, detail in result.defects
            )

        # -- energy domain -------------------------------------------------- #
        if energy_window is not None:
            for name in sorted(energy_window.channels):
                ch = energy_window.channels[name]
                eh = energy_health.get(name) or energy_health.get(ch.entity_id or "")
                result = self._score_energy_channel(name, ch, eh, unavailable_set, issues)
                status = (
                    QualityStatus.MISSING
                    if ch.value_kwh is None
                    else score_to_status(result.score, self.thresholds)
                )
                channels[name] = {
                    "domain": "energy",
                    "unit": "kWh",
                    "score": round(result.score, 4),
                    "status": status.value,
                    "existing_quality": ch.quality,
                    "value_w": None,
                    "value_kwh": ch.value_kwh,
                    "reliability_factor": round(result.score, 4),
                    "age_seconds": ch.age_seconds,
                    "repeated": ch.repeated,
                    "state_timestamp": ch.state_timestamp.isoformat()
                    if ch.state_timestamp
                    else None,
                    "received_at": ch.received_at.isoformat() if ch.received_at else None,
                    "sample_count": eh.sample_count if eh else 0,
                    "monotonic": eh.monotonic if eh else True,
                    "reset_count": eh.reset_count if eh else 0,
                    "max_drop_kwh": eh.max_drop_kwh if eh else 0.0,
                    "fresh": ch.available and not ch.is_stale,
                    "issues": list(result.issues),
                }
                if ch.value_kwh is not None:
                    scores.append(result.score)
                defects_to_log.extend(
                    (name, code, detail, sev) for code, sev, detail in result.defects
                )

        # Defensive path: energy-typed channels found inside a power window.
        for name in sorted(energy_channels):
            ch = energy_channels[name]
            result = self._score_energy_channel(name, ch, None, unavailable_set, issues)
            channels[name] = {
                "domain": "energy",
                "unit": "kWh",
                "score": round(result.score, 4),
                "status": (
                    QualityStatus.MISSING
                    if ch.value_w is None
                    else score_to_status(result.score, self.thresholds).value
                ),
                "existing_quality": ch.quality,
                "value_w": None,
                "value_kwh": ch.value_w,
                "reliability_factor": round(result.score, 4),
                "age_seconds": ch.age_seconds,
                "repeated": ch.repeated,
                "state_timestamp": ch.state_timestamp.isoformat() if ch.state_timestamp else None,
                "received_at": ch.received_at.isoformat() if ch.received_at else None,
                "fresh": ch.available and not ch.is_stale,
                "issues": list(result.issues),
            }
            if ch.value_w is not None:
                scores.append(result.score)

        overall = sum(scores) / len(scores) if scores else 0.0
        overall, rec_issues = self._apply_reconciliation(reconciliation, overall, issues, channels)
        if not scores and (power_channels or energy_channels):
            _issue(
                issues,
                None,
                "no_channels_available",
                f"{len(power_channels) + len(energy_channels)} requested",
            )
        overall = max(0.0, min(1.0, overall))

        report = DataQualityReport(
            timestamp=utcnow(),
            overall_score=round(overall, 4),
            channels=channels,
            issues=issues,
        )
        self._last_report = report
        self._last_issue_set = set(issues)
        self._last_clusters = clusters

        should_log = self.thresholds.log_events if log_events is None else log_events
        if should_log and self.db is not None:
            self._persist_events(defects_to_log, rec_issues)
        return report

    # -- power-domain scoring --------------------------------------------- #
    def _score_power_channel(
        self,
        name: str,
        ch: AlignedChannel,
        health: ChannelHealth | None,
        cfg: CTChannel | None,
        unavailable: set[str],
        issues: list[str],
        overlap_codes: tuple[tuple[str, str], ...] = (),
    ) -> _ChannelScore:
        t = self.thresholds
        result = _ChannelScore(name)
        entity_id = ch.entity_id or (cfg.entity_id if cfg is not None else None)
        is_unavailable = _is_unavailable(name, entity_id, cfg, unavailable)

        # 1. Missing / unavailable
        if ch.value_w is None:
            code = "sensor_unavailable" if is_unavailable else "missing_sample"
            result.penalize(
                t.missing_penalty, code, "no sample in window", severity="warning", issues=issues
            )
            return result
        if is_unavailable:
            result.penalize(
                t.stale_penalty, "sensor_unavailable", "HA reports state unavailable", issues=issues
            )

        # 2. Implausible value / non-finite (power only)
        if not math.isfinite(ch.value_w):
            result.penalize(
                1.0, "implausible_value", "non-finite value", severity="error", issues=issues
            )
            return result
        if abs(ch.value_w) > t.absurd_power_w:
            result.penalize(
                t.implausible_penalty,
                "implausible_value",
                f"{ch.value_w:.1f} W exceeds {t.absurd_power_w:.0f} W",
                severity="error",
                issues=issues,
            )

        # 3. Sample age
        age = ch.age_seconds
        expected = self._expected_for(name, health)
        if age is not None:
            if age > t.stale_after_s:
                result.penalize(t.stale_penalty, "stale_sample", f"age={age:.1f}s", issues=issues)
            elif expected and age > max(t.stale_after_s * 0.5, expected * t.max_age_factor):
                result.penalize(
                    t.age_penalty,
                    "age_high",
                    f"age={age:.1f}s expected~{expected:.1f}s",
                    issues=issues,
                )

        # 4. Timestamp consistency
        if ch.state_timestamp is not None and ch.received_at is not None:
            skew = (ch.received_at - ch.state_timestamp).total_seconds()
            if skew < -t.skew_warn_s:
                result.penalize(
                    t.future_skew_penalty,
                    "timestamp_future",
                    f"state timestamp {abs(skew):.1f}s in the future",
                    issues=issues,
                )
            elif abs(skew) > t.skew_bad_s:
                result.penalize(
                    t.skew_penalty, "timestamp_skew", f"skew={skew:.1f}s", issues=issues
                )
            elif abs(skew) > t.skew_warn_s:
                result.penalize(
                    t.skew_penalty * 0.5, "timestamp_skew", f"skew={skew:.1f}s", issues=issues
                )

        # 5. Duplicate / repeated sample
        if ch.repeated:
            result.penalize(
                t.repeated_penalty,
                "repeated_sample",
                "value unchanged; not a new physical measurement",
                issues=issues,
            )

        # 6. Interval anomalies and noise (power domain only)
        if health is not None:
            if health.interval_median_s and health.interval_median_s > 0:
                if (
                    health.interval_p95_s
                    and health.interval_p95_s / health.interval_median_s
                    > t.interval_jitter_ratio_warn
                ):
                    result.penalize(
                        t.interval_penalty,
                        "interval_jitter",
                        f"p95/median={health.interval_p95_s / health.interval_median_s:.1f}",
                        issues=issues,
                    )
                if expected and health.interval_median_s > expected * t.interval_slow_factor_warn:
                    result.penalize(
                        t.interval_penalty,
                        "interval_slow",
                        f"median={health.interval_median_s:.1f}s expected~{expected:.1f}s",
                        issues=issues,
                    )
            noise = health.noise_mad_w
            if noise is not None:
                threshold = max(t.noise_floor_w, abs(ch.value_w) * t.noise_relative_warn)
                if noise > threshold:
                    result.penalize(
                        t.noise_penalty,
                        "excessive_noise",
                        f"noise={noise:.2f}W threshold={threshold:.2f}W",
                        issues=issues,
                    )
            resolution = health.resolution_w
            if (
                resolution is not None
                and abs(ch.value_w) > 0
                and resolution > abs(ch.value_w) * t.resolution_relative_warn
                and resolution > 1.0
            ):
                result.penalize(
                    t.resolution_penalty,
                    "coarse_resolution",
                    f"resolution={resolution:.2f}W value={ch.value_w:.2f}W",
                    issues=issues,
                    )

        # 7. Polarity / phase consistency
        if cfg is not None:
            phase_value = getattr(cfg.phase, "value", cfg.phase)
            polarity_value = getattr(cfg.polarity, "value", cfg.polarity)
            if polarity_value == Polarity.INVERTED.value and ch.value_w > 0:
                result.penalize(
                    t.polarity_penalty,
                    "polarity_inconsistent",
                    "CT configured inverted but value is positive",
                    issues=issues,
                )
            # The reverse case: a load row declared ``normal`` that reports a
            # negative value.  A load cannot consume negative average power, so
            # either the CT is reversed or the declaration is wrong.  Left
            # unreported, the value is clipped to zero by the direct path and the
            # circuit silently disappears from every total while claiming
            # ``quality: good``.
            #
            # A *main* row is excluded on purpose: negative power there is real
            # export / reverse flow and is reported as such, not a defect.
            ct_type_value = getattr(cfg.ct_type, "value", cfg.ct_type)
            if (
                polarity_value == Polarity.NORMAL.value
                and ct_type_value in ("branch", "dedicated_device")
                and ch.value_w < 0
            ):
                result.penalize(
                    t.polarity_penalty,
                    "polarity_inconsistent",
                    f"load row configured normal but value is negative ({ch.value_w:.1f} W); "
                    "the CT is reversed or the polarity declaration is wrong",
                    issues=issues,
                )
            if phase_value == Phase.UNKNOWN.value:
                result.penalize(
                    t.phase_unknown_penalty, "phase_unknown", "topology phase unconfirmed", issues=issues
                )

        # 8. Measurement overlap — one penalty per cluster membership.
        seen_codes: set[str] = set()
        for code, detail in overlap_codes:
            if code in seen_codes:
                continue
            seen_codes.add(code)
            result.penalize(t.overlap_penalty, code, detail, issues=None)

        result.score = max(0.0, min(1.0, result.score))
        return result

    # -- energy-domain scoring --------------------------------------------- #
    def _score_energy_channel(
        self,
        name: str,
        ch: AlignedChannel | Any,
        health: EnergyHealth | None,
        unavailable: set[str],
        issues: list[str],
    ) -> _ChannelScore:
        t = self.thresholds
        result = _ChannelScore(name)
        value_kwh = getattr(ch, "value_kwh", None)
        if value_kwh is None:
            value_kwh = getattr(ch, "value_w", None) if getattr(ch, "is_energy", False) else None
        entity_id = getattr(ch, "entity_id", None)
        age = getattr(ch, "age_seconds", None)
        quality = getattr(ch, "quality", "good")
        repeated = getattr(ch, "repeated", False)

        if value_kwh is None:
            code = "sensor_unavailable" if entity_id in unavailable else "missing_sample"
            result.penalize(
                t.missing_penalty,
                code,
                "no energy sample in window",
                severity="warning",
                issues=issues,
            )
            return result
        if entity_id and entity_id in unavailable:
            result.penalize(
                t.stale_penalty,
                "sensor_unavailable",
                "HA reports state unavailable",
                issues=issues,
            )
        if not math.isfinite(value_kwh):
            result.penalize(
                1.0, "implausible_value", "non-finite energy counter", severity="error", issues=issues
            )
            return result
        if abs(value_kwh) > t.energy_absurd_kwh:
            result.penalize(
                t.implausible_penalty,
                "implausible_value",
                f"{value_kwh:.3f} kWh exceeds {t.energy_absurd_kwh:.0f} kWh",
                severity="error",
                issues=issues,
            )

        # Staleness — same thresholds, but messages stay in kWh.
        if age is not None:
            if age > t.stale_after_s:
                result.penalize(t.stale_penalty, "stale_sample", f"age={age:.1f}s", issues=issues)
            elif quality == QualityStatus.STALE.value:
                result.penalize(t.stale_penalty, "stale_sample", "energy sample is stale", issues=issues)

        if repeated:
            result.penalize(
                t.repeated_penalty,
                "repeated_sample",
                "energy counter unchanged; not a new increment",
                issues=issues,
            )

        # Counter reset detection (monotonicity).  Reported in kWh only.
        if health is not None:
            if health.reset_count > 0 and health.max_drop_kwh > t.energy_reset_min_drop_kwh:
                result.penalize(
                    t.energy_reset_penalty,
                    "energy_counter_reset",
                    f"counter decreased {health.max_drop_kwh:.6f} kWh "
                    f"({health.reset_count} reset(s)); possible meter reset or rollover",
                    severity="warning",
                    issues=issues,
                )
            if health.missing:
                result.penalize(
                    t.stale_penalty, "stale_sample", "no recent energy update", issues=issues
                )

        result.score = max(0.0, min(1.0, result.score))
        return result

    # -- overlap detection -------------------------------------------------- #
    def _power_overlap_issues(
        self,
        power_channels: Mapping[str, AlignedChannel],
        channel_config: Mapping[str, CTChannel],
    ) -> tuple[
        dict[str, tuple[tuple[str, str], ...]],
        list[OverlapCluster],
        list[str],
        list[tuple[str, str, str, str]],
    ]:
        """Detect duplicate entity bindings and identical simultaneous values.

        Returns ``(per_channel_codes, clusters, issue_lines, logged_events)``.
        Identical-value clusters produce exactly **one** issue each, listing all
        members and the cluster size.
        """
        t = self.thresholds
        per_channel: dict[str, list[tuple[str, str]]] = {}
        issues: list[str] = []
        logged: list[tuple[str, str, str, str]] = []

        # 1. Same HA entity bound to several channels.
        by_entity: dict[str, list[str]] = {}
        for name, ch in power_channels.items():
            cfg = channel_config.get(name)
            entity = cfg.entity_id if cfg is not None else ch.entity_id
            if entity:
                by_entity.setdefault(entity, []).append(name)
        for entity, names in sorted(by_entity.items()):
            if len(names) <= 1:
                continue
            members = tuple(sorted(names))
            for name in members:
                per_channel.setdefault(name, []).append(
                    ("duplicate_entity", f"{entity} bound to {len(members)} channels")
                )
            issues.append(
                "duplicate_entity (entity="
                f"{entity}, size={len(members)}, members={','.join(members)}): "
                + DUPLICATE_ENTITY_EXPLANATION
            )
            for name in members:
                logged.append((name, "duplicate_entity", f"members={len(members)}", "warning"))

        # 2. Identical simultaneous power values across distinct entities.
        seen: dict[tuple[float, int], list[str]] = {}
        for name, ch in power_channels.items():
            if ch.value_w is None or ch.received_at is None or ch.is_energy:
                continue
            if not math.isfinite(ch.value_w):
                continue
            key = (round(ch.value_w, 3), int(ch.received_at.timestamp()))
            seen.setdefault(key, []).append(name)
        clusters: list[OverlapCluster] = []
        for (value, ts), names in sorted(seen.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if len(names) < max(2, t.overlap_min_cluster) or abs(value) <= t.overlap_min_abs_w:
                continue
            members = tuple(sorted(names))
            cluster = OverlapCluster(channels=members, value=value, timestamp_s=ts, domain="power")
            clusters.append(cluster)
            detail = (
                f"identical value {value:.3f} W at the same instant across "
                f"{len(members)} entities (size={len(members)}, "
                f"members={','.join(members)}): " + OVERLAP_EXPLANATION
            )
            issues.append(f"measurement_overlap ({detail})")
            for name in members:
                per_channel.setdefault(name, []).append(
                    ("measurement_overlap", f"cluster size {len(members)}")
                )
                logged.append(
                    (name, "measurement_overlap", f"cluster_size={len(members)} value={value:.3f}W", "warning")
                )
        return (
            {name: tuple(codes) for name, codes in per_channel.items()},
            clusters,
            issues,
            logged,
        )

    def _apply_reconciliation(
        self,
        reconciliation: Reconciliation | None,
        overall: float,
        issues: list[str],
        channels: dict[str, dict[str, Any]],
    ) -> tuple[float, list[tuple[str, str, str, str]]]:
        logged: list[tuple[str, str, str, str]] = []
        if reconciliation is None:
            return overall, logged
        t = self.thresholds
        ratio = abs(reconciliation.reconciliation_error_ratio)
        if ratio > t.reconciliation_bad_ratio:
            overall = max(0.0, overall - t.reconciliation_penalty)
            _issue(issues, None, "reconciliation_error", f"ratio={ratio:.3f}")
            logged.append(("reconciliation", "reconciliation_error", f"ratio={ratio:.3f}", "error"))
        elif ratio > t.reconciliation_warn_ratio:
            overall = max(0.0, overall - t.reconciliation_penalty * 0.5)
            _issue(issues, None, "reconciliation_error", f"ratio={ratio:.3f}")
            logged.append(("reconciliation", "reconciliation_error", f"ratio={ratio:.3f}", "warning"))
        return overall, logged

    # -- persistence ------------------------------------------------------- #
    def _persist_events(
        self,
        defects: list[tuple[str, str, str, str]],
        rec_issues: list[tuple[str, str, str, str]],
    ) -> None:
        if self.db is None:
            return
        now = utcnow()
        for channel, code, detail, severity in list(defects) + list(rec_issues):
            key = (channel, code)
            last = self._last_logged.get(key)
            if last is not None and (now - last).total_seconds() < self.thresholds.log_cooldown_s:
                continue
            self._last_logged[key] = now
            try:
                self.db.log_quality_event(
                    code=code,
                    message=f"{channel}: {detail}"[:1000],
                    severity=severity,
                    channel_id=channel,
                    details={"detail": detail},
                )
            except Exception:  # noqa: BLE001 - logging must never break evaluation
                self.log.exception("failed to persist data quality event %s/%s", channel, code)

    # -- accessors --------------------------------------------------------- #
    @property
    def last_report(self) -> DataQualityReport | None:
        return self._last_report

    @property
    def last_overlap_clusters(self) -> list[OverlapCluster]:
        return getattr(self, "_last_clusters", [])

    def status_for(self, channel: str) -> QualityStatus:
        report = self._last_report
        if report is None:
            return QualityStatus.MISSING
        entry = report.channels.get(channel)
        if not entry:
            return QualityStatus.MISSING
        try:
            return QualityStatus(entry.get("status", "missing"))
        except ValueError:
            return QualityStatus.BAD


__all__ = [
    "DataQualityEngine",
    "DUPLICATE_ENTITY_EXPLANATION",
    "OVERLAP_EXPLANATION",
    "OverlapCluster",
    "QUALITY_THRESHOLDS",
    "QualityThresholds",
    "score_to_status",
]
