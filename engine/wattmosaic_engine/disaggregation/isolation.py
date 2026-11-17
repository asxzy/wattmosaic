"""Event isolation: was an event's power window attributable to one device?

The requirement (brief §10-11, Group G) is not "did an event happen?" but
"when this device changed, was anything else changing in the same window?".
A blend of numbers cannot answer that, so :class:`EventIsolator` produces a
named :class:`~wattmosaic_engine.contracts.EventIsolation` verdict with the
window that was examined and the evidence that decided it.

Two windows, deliberately separate
---------------------------------
The isolation window is the *tight* claim: "this device's own transition is
the only physical device change around the event".  The power-analysis window
is the *wide* claim: "the CT delta attributed to this event was not polluted
by a later sibling step".  They are not the same length: the brief forbids
assuming one fixed duration is optimal.  A hot tub's compressor may be quiet
for a minute while a lamp's step is over in a second.  Configuration is
therefore resolved in three layers::

    per-device override  >  device-class default  >  global default

and every layer is validated (non-negative, finite).  ``analysis_offset_s``
shifts the window forward when the physical response lags the HA event; the
original ``occurred_at`` is *never* rewritten -- isolation analysis adds
metadata, it does not move history.

Decision table (first match wins)
--------------------------------
==================================================  =================  =========================
condition                                           status             reason
==================================================  =================  =========================
the subject change is not load evidence             INSUFFICIENT_DATA  not_load_evidence
``occurred_at`` has no timezone                     INSUFFICIENT_DATA  event_time_naive
another *physical device* changed in the            CONTAMINATED       concurrent_device_change
  isolation window
another *physical device* changed in the            CONTAMINATED       power_window_device_change
  power window only
coverage was not measured                           INSUFFICIENT_DATA  coverage_unknown
coverage is measured but too thin                   INSUFFICIENT_DATA  insufficient_samples
``now`` precedes the end of the window              INSUFFICIENT_DATA  window_not_elapsed
otherwise                                           ISOLATED           isolated
==================================================  =================  =========================

Contamination is decided *before* the coverage checks because it is positive
evidence: seeing a sibling move needs no CT samples to believe.  Everything
else defaults to ``INSUFFICIENT_DATA`` -- absence of data is never promoted to
``ISOLATED``.

Verdict vs guard: which competitor can actually pollute a delta
--------------------------------------------------------------
``classify`` answers the literal question -- "when this device changed, was
anything else changing in the same window?" -- and that stays independent of
how many meters exist.  Whether a competitor could have moved *a particular*
measured delta is a narrower question, answered by
:func:`could_share_ct` and recorded as ``evidence["ct_separated_device_ids"]``:
a competitor whose declared ``supporting_ct_ids`` are disjoint from the
subject's cannot have moved that reading.  Only a *positive* declaration makes
a competitor harmless; unknown scope on either side still interferes, so the
default stays conservative.  The learning guard uses that finding to avoid
discarding the only clean samples a dedicated-CT appliance ever produces
during an automation scene.

Physical-device identity (many entities, one appliance)
------------------------------------------------------
``physical_device_events`` groups HA events by ``device_id`` using the typed
``DeviceConfiguration.entity_bindings`` roles from contract 1.6.0:

* an entity bound with ``is_load_evidence=True`` (or role ``LOAD_STATE`` /
  ``POWER_MEASUREMENT``) counts as a load-change trigger,
* ``AVAILABILITY`` / ``ENERGY_COUNTER`` / ``MODE_ONLY`` / ``UNKNOWN`` never
  create a load event,
* an *unbound* entity is treated as load evidence: the absence of a binding is
  not proof that an entity only reports reachability,
* one device yields exactly one event, so three entities firing together are
  **one** competing device, not three.  When two entities disagree we keep
  both observations inside the one event and mark ``conflicting``,
* at most one ``contributes_power`` entity per device is honoured; a broken
  configuration with two is reported (``power_contributor_conflict``) rather
  than double-counted.

Retention
---------
A contaminated event is *kept*.  ``split_attribution`` partitions isolations
into clean attribution inputs and withheld ones inside one list; nothing is
deleted, and ``clean_attribution_inputs`` is the only exclusion point.

Out-of-order arrival
--------------------
Nothing orders by arrival.  Groups and windows are ordered by ``occurred_at``,
and ``reclassify`` lets a late event retroactively contaminate a verdict that
was already ``ISOLATED``.  ``occurred_at`` stays byte-identical throughout.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from wattmosaic_engine.contracts import (
    CONTRACT_VERSION,
    EventIsolation,
    HaEntityBinding,
    HaEntityRole,
    IsolationStatus,
    utcnow,
)
from wattmosaic_engine.synchronization.aligner import (
    CADENCE_SOURCE_DEFAULT,
    CADENCE_SOURCE_OBSERVED,
    ResolvedCadence,
    resolve_channel_cadence,
    resolve_topology_cadence,
)

#: Roles that prove a physical load changed.  ``MODE_ONLY`` and
#: ``ENERGY_COUNTER`` deliberately absent: a mode signal and a cumulative meter
#: do not tell us the appliance drew power *at this instant*.
LOAD_EVIDENCE_ROLES = frozenset({HaEntityRole.LOAD_STATE, HaEntityRole.POWER_MEASUREMENT})

#: The three event kinds (spec 07 §2).  Every event names exactly one; the
#: values are the spec's names verbatim so a consumer can compare them.
EVENT_KIND_HA = "ha_driven"
EVENT_KIND_MEASUREMENT = "measurement_driven"
EVENT_KIND_UNKNOWN_RESIDUAL = "unknown_residual"

#: The closed set.  A marker outside it is a defect, not a fourth kind.
EVENT_KINDS = frozenset(
    {EVENT_KIND_HA, EVENT_KIND_MEASUREMENT, EVENT_KIND_UNKNOWN_RESIDUAL}
)

#: The versioned marker an unknown-residual producer writes to name its own
#: kind.  It carries the contract version so a payload written by a newer
#: producer is still recognisable; the bare forms in :data:`EXPLICIT_KIND_MARKERS`
#: are accepted so a producer is not forced to know the version.  Defined before
#: its first use below so the module imports cleanly.
UNKNOWN_RESIDUAL_MARKER = f"{EVENT_KIND_UNKNOWN_RESIDUAL}:{CONTRACT_VERSION}"

#: Explicit marker values a producer may write to name its own kind.  The
#: unknown-residual marker carries the contract version so a payload written by
#: a newer producer is still recognisable; the bare forms are accepted so a
#: producer is not forced to know the version.
EXPLICIT_KIND_MARKERS: Mapping[str, str] = {
    UNKNOWN_RESIDUAL_MARKER: EVENT_KIND_UNKNOWN_RESIDUAL,
    "unknown_residual": EVENT_KIND_UNKNOWN_RESIDUAL,
    "residual": EVENT_KIND_UNKNOWN_RESIDUAL,
    "ha_driven": EVENT_KIND_HA,
    "measurement_driven": EVENT_KIND_MEASUREMENT,
}

#: ``event_type`` -> kind for an HA-produced :class:`DeviceEvent`.  A CT power
#: entity's ``power_change`` is still HA-*driven*: the source is the Home
#: Assistant entity, not the step detector (spec 07 §2.1/§2.2).
HA_EVENT_TYPE_KINDS: Mapping[str, str] = {
    "state_changed": EVENT_KIND_HA,
    "power_change": EVENT_KIND_HA,
    "cycle_start": EVENT_KIND_HA,
    "stage_change": EVENT_KIND_HA,
    "cycle_end": EVENT_KIND_HA,
    "availability": EVENT_KIND_HA,
}

#: Where a kind was resolved from, published beside it so a consumer can tell a
#: producer's own declaration from a classification made by the engine.
KIND_SOURCE_EXPLICIT = "explicit_marker"
KIND_SOURCE_EVENT_TYPE = "event_type"
KIND_SOURCE_INFERRED = "inferred_source"


def event_kind(obj: Any) -> str:
    """Resolve one observation's kind -- always exactly one of the three.

    Resolution order, first match wins:

    1. an explicit ``kind`` / ``event_kind`` marker naming one of
       :data:`EVENT_KINDS` -- that kind as written by the producer;
    2. the ``event_type`` vocabulary (:data:`HA_EVENT_TYPE_KINDS`)
       -- :data:`EVENT_KIND_HA`;
    3. a ``channel`` / ``ct_id`` / ``magnitude_delta`` / signed ``delta_w``
       payload, or ``source`` naming ``ct_step`` / ``step_detection``
       -- :data:`EVENT_KIND_MEASUREMENT`;
    4. otherwise :data:`EVENT_KIND_HA`.

    Step 4 is honest rather than generous: every producer this module reads is
    either an HA state change or a CT step, and an HA-shaped observation that
    carries neither signal is still a Home-Assistant-driven observation.  What
    the function may *never* return is "unknown": an event with no kind is a
    defect, so callers that need the distinction read :func:`event_kind_source`
    as well.
    """
    marker = _field(obj, "kind", _field(obj, "event_kind"))
    if isinstance(marker, str):
        text = marker.strip()
        if text in EXPLICIT_KIND_MARKERS:
            return EXPLICIT_KIND_MARKERS[text]
        if text in EVENT_KINDS:
            return text
    event_type = _field(obj, "event_type")
    if isinstance(event_type, str):
        kind = HA_EVENT_TYPE_KINDS.get(event_type.strip())
        if kind is not None:
            return kind
    if _has_measurement_shape(obj):
        return EVENT_KIND_MEASUREMENT
    return EVENT_KIND_HA


def event_kind_source(obj: Any) -> str:
    """Where :func:`event_kind`'s answer came from.

    ``explicit_marker`` means the producer wrote the kind itself; ``event_type``
    means the HA vocabulary decided it; ``inferred_source`` means the payload's
    shape did.  Published with the kind so a consumer can audit it.
    """
    marker = _field(obj, "kind", _field(obj, "event_kind"))
    if isinstance(marker, str):
        text = marker.strip()
        if text in EXPLICIT_KIND_MARKERS or text in EVENT_KINDS:
            return KIND_SOURCE_EXPLICIT
    event_type = _field(obj, "event_type")
    if isinstance(event_type, str) and event_type.strip() in HA_EVENT_TYPE_KINDS:
        return KIND_SOURCE_EVENT_TYPE
    return KIND_SOURCE_INFERRED


def _has_measurement_shape(obj: Any) -> bool:
    """Does this observation carry a reader-side (CT step) payload?"""
    for name in ("channel", "ct_id"):
        if _field(obj, name) not in (None, ""):
            return True
    if _field(obj, "magnitude_delta") is not None:
        return True
    raw = _field(obj, "delta_w")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return True
    source = _field(obj, "source")
    if isinstance(source, str):
        lowered = source.strip().lower()
        if "ct_step" in lowered or "step_detection" in lowered or "step_detector" in lowered:
            return True
    return False

#: State labels that mean "the appliance is running" / "not running".  Local
#: copies of the learning filter's labels on purpose: importing
#: ``learning.sample_filter`` here would create a cycle (the learning pipeline
#: imports this module).
ON_LABELS = frozenset({"on", "open", "running", "active", "heating", "cooling", "true", "1"})
OFF_LABELS = frozenset({"off", "closed", "idle", "standby", "inactive", "false", "0"})

#: ``main.py`` emits this event type for reachability flips.
AVAILABILITY_EVENT_TYPE = "availability"

#: State labels that mean "we do not know whether the appliance is running".
#: ``HASource`` reports these as availability rather than state, but the
#: production bridge (``_bridge_event``) drops ``event_type`` when it converts
#: an event to the learning dict, so the label is the only surviving signal.
#: A reachability flip says nothing about watts and must never be counted as a
#: load change.
NON_LOAD_LABELS = frozenset({"unavailable", "unknown", "none"})

REASON_ISOLATED = "isolated"
REASON_CONCURRENT = "concurrent_device_change"
REASON_POWER_WINDOW = "power_window_device_change"
REASON_COVERAGE_UNKNOWN = "coverage_unknown"
REASON_INSUFFICIENT_SAMPLES = "insufficient_samples"
REASON_WINDOW_NOT_ELAPSED = "window_not_elapsed"
REASON_NOT_LOAD_EVIDENCE = "not_load_evidence"
REASON_NAIVE_TIME = "event_time_naive"
REASON_LATE = "late_competing_device"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def _check_timing(name: str, value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite (got {value!r})")
    if number < 0.0:
        raise ValueError(f"{name} must be non-negative (got {number})")
    return number


@dataclass(frozen=True, slots=True)
class IsolationWindow:
    """One device's analysis timings, in seconds.

    ``power_window_s`` may be omitted, in which case it equals
    ``isolation_window_s``.  It is never allowed to be *shorter* than the
    isolation window: a power window that excluded part of the isolation window
    would hide a change the tight check already saw.
    """

    analysis_offset_s: float = 0.0
    isolation_window_s: float = 30.0
    power_window_s: float | None = None

    def __post_init__(self) -> None:
        offset = _check_timing("analysis_offset_s", self.analysis_offset_s)
        window = _check_timing("isolation_window_s", self.isolation_window_s)
        object.__setattr__(self, "analysis_offset_s", offset)
        object.__setattr__(self, "isolation_window_s", window)
        if self.power_window_s is None:
            object.__setattr__(self, "power_window_s", window)
        else:
            power = _check_timing("power_window_s", self.power_window_s)
            object.__setattr__(self, "power_window_s", max(window, power))

    @property
    def analysis_window_s(self) -> float:
        """Width of the wide window inspected for power contamination."""
        power = self.power_window_s
        return float(power if power is not None else self.isolation_window_s)

    @classmethod
    def merged(cls, base: IsolationWindow, overrides: Mapping[str, Any]) -> IsolationWindow:
        """One override map applied on top of ``base``.

        Only known timing keys are read, so a configuration typo cannot move an
        unrelated timing, and non-finite/negative values are dropped instead of
        producing a window that silently never matches.
        """
        values: dict[str, float] = {
            "analysis_offset_s": base.analysis_offset_s,
            "isolation_window_s": base.isolation_window_s,
            "power_window_s": base.analysis_window_s,
        }
        for key in ("analysis_offset_s", "isolation_window_s", "power_window_s"):
            if key not in overrides:
                continue
            try:
                number = float(overrides[key])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number) or number < 0.0:
                continue
            values[key] = number
        return cls(**values)

    def shifted(self, occurred_at: datetime) -> tuple[datetime, datetime, datetime]:
        """Return ``(window_start, window_end, power_window_end)``.

        The offset is applied to the window, never to ``occurred_at``.
        """
        start = occurred_at + timedelta(seconds=self.analysis_offset_s)
        return (
            start,
            start + timedelta(seconds=self.isolation_window_s),
            start + timedelta(seconds=self.analysis_window_s),
        )


#: Where a *derived* window came from.  The vocabulary is deliberately separate
#: from :meth:`IsolationConfig.resolve`'s ``device`` / ``class`` / ``global``
#: (that one names the configuration layer that answered).  These name how the
#: number was obtained.
#:
#: Two different ``"configured"* values are published by this codebase and they
#: must not be conflated: :data:`WINDOW_SOURCE_CONFIGURED` (here) is the width
#: an operator set, while ``aligner.CADENCE_SOURCE_DEFAULT``
#: (``"configured_default"``) is the *cadence* fallback a reader with no usable
#: history gets.  One is a user declaration about an appliance, the other a
#: documented default about a sampling rate.
WINDOW_SOURCE_CONFIGURED = "configured"
WINDOW_SOURCE_OBSERVED = CADENCE_SOURCE_OBSERVED

#: How many cadences the global isolation window spans when the observed
#: cadence is used.  Two intervals is the smallest spacing a channel can show,
#: so a window narrower than two cadences could never hold the evidence it asks
#: for; two gives the documented behaviour at the audited 15.7 s cadence
#: (~31.4 s against the shipped 30 s) while still being a derivation rather
#: than a constant.
WINDOW_CADENCES = 2.0

#: Upper bound on the derived isolation window, and on the power window derived
#: from it.  The same clamp the step detector applies to its own cadence windows
#: (``disaggregation/events.py``): one pathological cadence must not produce a
#: minutes-long event window.  The shipped configured widths stay the floor, so
#: an unusual reader can only widen a window, never narrow it below the
#: documented default.
MAX_DERIVED_WINDOW_S = 120.0


@dataclass(frozen=True, slots=True)
class _CadencePoint:
    """The one field :func:`resolve_channel_cadence` reads: arrival time.

    The aligner resolves a cadence from ``entry.received_at``.  Isolation
    receives ``(received_at, value_w)`` pairs, so the value is carried only to
    keep the caller's shape; the cadence depends on the timestamps alone.
    """

    received_at: datetime
    value_w: float


def window_from_cadence(
    cadence: float,
    *,
    default_s: float = 30.0,
    power_s: float = 60.0,
    samples: int = 0,
) -> tuple[float, float, str]:
    """Derive ``(isolation_window_s, power_window_s, source)`` from a cadence.

    A channel with fewer than :data:`MIN_CADENCE_INTERVALS` observed gaps has not
    shown a repeatable spacing (the aligner's own rule), so its cadence is the
    documented fallback and the configured widths stay, labelled
    :data:`WINDOW_SOURCE_CONFIGURED`.  Otherwise the isolation window is
    :data:`WINDOW_CADENCES` cadences wide, clamped into
    ``[default_s, MAX_DERIVED_WINDOW_S]``, and the power window keeps the
    configured ratio (twice the isolation window) up to the same ceiling.

    The derivation is total: every input produces a window, so an unobserved
    channel can never yield a zero-width window.
    """
    try:
        value = float(cadence)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value) or value <= 0.0 or samples < 2:
        return float(default_s), float(power_s), WINDOW_SOURCE_CONFIGURED
    derived = min(max(WINDOW_CADENCES * value, float(default_s)), MAX_DERIVED_WINDOW_S)
    ratio = (float(power_s) / float(default_s)) if float(default_s) > 0 else 1.0
    power = min(max(derived * ratio, derived), MAX_DERIVED_WINDOW_S)
    return derived, power, WINDOW_SOURCE_OBSERVED


@dataclass(frozen=True, slots=True)
class ResolvedWindow:
    """A resolved isolation window, its width and where the width came from."""

    window: IsolationWindow
    source: str
    cadence_source: str
    cadence_seconds: float | None = None
    cadence_intervals: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "analysis_offset_s": self.window.analysis_offset_s,
            "isolation_window_s": self.window.isolation_window_s,
            "power_window_s": self.window.analysis_window_s,
            "source": self.source,
            "cadence_source": self.cadence_source,
            "cadence_seconds": self.cadence_seconds,
            "cadence_intervals": self.cadence_intervals,
        }


@dataclass(slots=True)
class IsolationConfig:
    """Global isolation timings plus optional class and per-device overrides.

    ``defaults`` is keyed by a device-class label (``PowerBehavior`` /
    ``DeviceType`` value, or any label the caller uses); ``per_device`` is keyed
    by ``device_id``.  Both are optional: an empty config is the documented
    global default, not a claim that one fixed duration fits every appliance.
    """

    analysis_offset_s: float = 0.0
    isolation_window_s: float = 30.0
    power_window_s: float = 60.0
    #: Optional observed-cadence history, ``channel -> [(received_at, value_w)]``.
    #: When supplied (and non-empty) the global window is *derived* from it by
    #: :func:`window_from_cadence` instead of being read as a constant.  This is
    #: the wave-1.2 cadence publication consumed at the point of use, so the
    #: resolved window can say whether it was measured or configured.
    cadence_samples: Mapping[str, Sequence[tuple[datetime, float]]] = field(
        default_factory=dict
    )
    #: Minimum usable readings on each side before a verdict is trustworthy.
    min_baseline_samples: int = 3
    min_event_samples: int = 3
    #: ``device class label -> timings`` (the middle layer).
    defaults: Mapping[str, IsolationWindow] = field(default_factory=dict)
    #: ``device_id -> timings`` (the strongest layer).
    per_device: Mapping[str, IsolationWindow] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.analysis_offset_s = _check_timing("analysis_offset_s", self.analysis_offset_s)
        self.isolation_window_s = _check_timing("isolation_window_s", self.isolation_window_s)
        self.power_window_s = _check_timing("power_window_s", self.power_window_s)
        if self.power_window_s < self.isolation_window_s:
            self.power_window_s = self.isolation_window_s
        for value, name in (
            (self.min_baseline_samples, "min_baseline_samples"),
            (self.min_event_samples, "min_event_samples"),
        ):
            if int(value) < 0:
                raise ValueError(f"{name} must be non-negative (got {value!r})")
        self.min_baseline_samples = int(self.min_baseline_samples)
        self.min_event_samples = int(self.min_event_samples)
        # Validate overrides eagerly: a negative window discovered at classify
        # time would surface as a wrong verdict, not as a config error.
        merged = {**dict(self.defaults), **dict(self.per_device)}
        for key, window in merged.items():
            if not isinstance(window, IsolationWindow):
                raise ValueError(f"isolation override for {key!r} must be an IsolationWindow")

    @property
    def global_window(self) -> IsolationWindow:
        return IsolationWindow(
            analysis_offset_s=self.analysis_offset_s,
            isolation_window_s=self.isolation_window_s,
            power_window_s=self.power_window_s,
        )

    def resolve(
        self, device_id: str, device_classes: Sequence[str] = ()
    ) -> tuple[IsolationWindow, str]:
        """Resolve timings for one device and say which layer answered.

        Returns ``(window, source)`` where ``source`` is ``"device"``,
        ``"class"`` or ``"global"``.  The source is recorded in the evidence so
        a verdict can be explained without reading the config.
        """
        override = self.per_device.get(device_id)
        if override is not None:
            return override, "device"
        for label in device_classes:
            found = self.defaults.get(str(label))
            if found is not None:
                return found, "class"
        return self.global_window, "global"

    # -- observed-cadence sizing ------------------------------------------ #
    def topology_cadence(self) -> ResolvedCadence:
        """The topology cadence implied by :attr:`cadence_samples`.

        Thin channels (fewer than ``MIN_CADENCE_INTERVALS`` gaps) are excluded,
        exactly as ``SampleAligner.topology_cadence`` excludes them, so a
        channel on the configured fallback cannot vote a default into the merge.
        """
        per_channel: dict[str, list[Any]] = {}
        for channel, entries in self.cadence_samples.items():
            rows: list[Any] = []
            for stamp, _watts in entries:
                rows.append(_CadencePoint(stamp, 0.0))
            if rows:
                per_channel[str(channel)] = rows
        return resolve_topology_cadence(
            {name: resolve_channel_cadence(rows) for name, rows in per_channel.items()}
        )

    def resolved_window(
        self, device_id: str, device_classes: Sequence[str] = ()
    ) -> ResolvedWindow:
        """Resolve one device's window *and* publish where the width came from.

        The configuration layer still answers first: a ``device`` or ``class``
        override is the user's declaration and is never second-guessed by a
        cadence.  Only the global layer consults the observed cadence, because
        that is the shipped default -- the number a fresh install would
        otherwise read off a constant.  The returned :class:`ResolvedWindow`
        therefore says both which layer answered (``source``) and whether the
        width was measured or configured (``cadence_source``), so a consumer can
        tell an observation from a default without reading the config.
        """
        override = self.per_device.get(device_id)
        if override is not None:
            return ResolvedWindow(
                window=override,
                source="device",
                cadence_source=WINDOW_SOURCE_CONFIGURED,
            )
        for label in device_classes:
            found = self.defaults.get(str(label))
            if found is not None:
                return ResolvedWindow(
                    window=found,
                    source="class",
                    cadence_source=WINDOW_SOURCE_CONFIGURED,
                )
        if not self.cadence_samples:
            return ResolvedWindow(
                window=self.global_window,
                source="global",
                cadence_source=WINDOW_SOURCE_CONFIGURED,
            )
        cadence = self.topology_cadence()
        derived_s, power_s, source = window_from_cadence(
            cadence.seconds,
            default_s=self.isolation_window_s,
            power_s=self.power_window_s,
            samples=cadence.intervals,
        )
        window = IsolationWindow(
            analysis_offset_s=self.analysis_offset_s,
            isolation_window_s=derived_s,
            power_window_s=power_s,
        )
        return ResolvedWindow(
            window=window,
            source="global",
            cadence_source=source,
            cadence_seconds=cadence.seconds,
            cadence_intervals=cadence.intervals,
        )


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SampleCoverage:
    """How much CT evidence was actually available for the window.

    ``None`` means *not measured*, which is different from zero and is never
    treated as sufficient.
    """

    baseline_samples: int | None = None
    event_samples: int | None = None

    def sufficient(self, *, min_baseline: int = 3, min_event: int = 3) -> bool:
        if self.baseline_samples is None or self.event_samples is None:
            return False
        return self.baseline_samples >= min_baseline and self.event_samples >= min_event

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline_samples": self.baseline_samples,
            "event_samples": self.event_samples,
        }


def _coerce_coverage(
    coverage: SampleCoverage | Mapping[str, Any] | Any | None,
) -> SampleCoverage | None:
    if coverage is None or isinstance(coverage, SampleCoverage):
        return coverage
    if isinstance(coverage, Mapping):
        return SampleCoverage(
            baseline_samples=coverage.get("baseline_samples", coverage.get("baseline")),
            event_samples=coverage.get("event_samples", coverage.get("event")),
        )
    baseline = getattr(coverage, "baseline_samples", None)
    event = getattr(coverage, "event_samples", None)
    if baseline is None and event is None:
        return None
    return SampleCoverage(baseline_samples=baseline, event_samples=event)


# --------------------------------------------------------------------------- #
# Physical device events
# --------------------------------------------------------------------------- #


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _identity(obj: Any, device_id: str, occurred_at: datetime) -> str:
    """A stable event id, synthesised only when the source has none.

    Production transitions reach the learning path as plain dicts bridged from
    ``main.py``; those carry no ``event_id``.  ``EventIsolation`` requires a
    contaminated verdict to *name* a competing event, so a missing id would turn
    a real verdict into a validation error.  The fallback is deterministic and
    self-describing (``device@timestamp``) rather than an invented UUID, so two
    runs over the same input produce the same evidence.
    """
    raw = _field(obj, "event_id")
    if raw not in (None, ""):
        return str(raw)
    return f"{device_id}@{_aware(occurred_at).isoformat()}"


def _state_label(obj: Any) -> str | None:
    raw = _field(obj, "new_state", _field(obj, "state"))
    return None if raw is None else str(raw).strip().lower()


def _state_polarity(obj: Any) -> bool | None:
    label = _state_label(obj)
    if label is None:
        return None
    if label in ON_LABELS:
        return True
    if label in OFF_LABELS:
        return False
    return None


def _ct_ids(obj: Any) -> frozenset[str]:
    """CTs whose readings this observation can move.

    ``_bridge_event`` attaches ``attributes["supporting_ct_ids"]`` from the
    topology, so two devices on *dedicated* CTs cannot pollute each other's
    measured delta even when they switch at the same instant.  An empty result
    means "not declared", which is treated conservatively by the caller: unknown
    scope never makes a competitor harmless.
    """
    attributes = _field(obj, "attributes")
    if not isinstance(attributes, Mapping):
        return frozenset()
    raw = attributes.get("supporting_ct_ids") or attributes.get("ct_ids") or ()
    if isinstance(raw, str):
        raw = (raw,)
    return frozenset(str(item) for item in raw if item not in (None, ""))


def could_share_ct(subject_cts: Iterable[str], other: Any) -> bool:
    """Could ``other`` have moved the reading measured on ``subject_cts``?

    ``other`` may be a :class:`PhysicalDeviceEvent` or its ``as_dict`` form.  An
    empty scope on *either* side means "not declared", which is assumed to
    interfere: only a positive, disjoint pair is harmless.  Sharing a CT is the
    normal case for a branch circuit; *not* sharing one is what makes a
    dedicated-CT device attributable even during an automation scene.
    """
    own = frozenset(str(item) for item in subject_cts if item not in (None, ""))
    if not own:
        return True
    raw = other.get("ct_ids") if isinstance(other, Mapping) else getattr(other, "ct_ids", None)
    theirs = frozenset(str(item) for item in (raw or ()) if item not in (None, ""))
    if not theirs:
        return True
    return bool(own & theirs)


def _is_load_change(obj: Any, binding: HaEntityBinding | None) -> bool:
    """Does this observation prove the physical load changed?

    ``binding is None`` means the entity was never declared.  That is not proof
    it is availability-only, so it counts -- the opposite default (ignore
    everything unbound) would make isolation blind to undeclared devices.

    Reachability is rejected on *either* signal available: the explicit
    ``event_type`` (``HASource`` sets it) or an ``unavailable``/``unknown``
    label (the only thing the production bridge keeps).
    """
    if str(_field(obj, "event_type", "state_changed")) == AVAILABILITY_EVENT_TYPE:
        return False
    if _state_label(obj) in NON_LOAD_LABELS:
        return False
    if binding is None:
        return True
    if binding.role in LOAD_EVIDENCE_ROLES:
        return True
    return bool(binding.is_load_evidence)


@dataclass(frozen=True, slots=True)
class PhysicalDeviceEvent:
    """One physical appliance's change, however many HA entities reported it."""

    device_id: str
    occurred_at: datetime
    received_at: datetime
    event_ids: tuple[str, ...]
    entity_ids: tuple[str, ...]
    states: Mapping[str, str]
    previous_states: Mapping[str, str]
    events: tuple[Any, ...]
    power_contributor: str | None = None
    conflicting: bool = False
    notes: tuple[str, ...] = ()
    #: CTs this device's readings can move.  Empty means "the topology did not
    #: declare a scope", which is never read as "cannot interfere".
    ct_ids: frozenset[str] = frozenset()

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "occurred_at": self.occurred_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "event_ids": list(self.event_ids),
            "entity_ids": list(self.entity_ids),
            "states": dict(self.states),
            "power_contributor": self.power_contributor,
            "conflicting": self.conflicting,
            "notes": list(self.notes),
            "ct_ids": sorted(self.ct_ids),
        }


def _bindings_by_entity(bindings: Iterable[HaEntityBinding]) -> dict[str, HaEntityBinding]:
    out: dict[str, HaEntityBinding] = {}
    for binding in bindings:
        out.setdefault(str(binding.entity_id), binding)
    return out


def _contributors_by_device(
    bindings: Iterable[HaEntityBinding],
) -> tuple[dict[str, str], set[str]]:
    """``device_id -> contributing entity`` plus devices with a broken config."""
    chosen: dict[str, str] = {}
    conflicted: set[str] = set()
    for binding in bindings:
        if not binding.contributes_power:
            continue
        device = str(binding.device_id)
        if device in chosen:
            conflicted.add(device)
            continue
        chosen[device] = str(binding.entity_id)
    return chosen, conflicted


def physical_device_events(
    events: Iterable[Any],
    bindings: Iterable[HaEntityBinding] = (),
) -> list[PhysicalDeviceEvent]:
    """Collapse HA entities into one event per physical device.

    Ordering is by ``occurred_at`` (never by arrival); ties keep first-seen
    order so the result is deterministic for a fixed input.
    """
    binding_list = list(bindings)
    by_entity = _bindings_by_entity(binding_list)
    contributors, contributor_conflicts = _contributors_by_device(binding_list)

    buckets: dict[str, list[Any]] = {}
    first_seen: dict[str, int] = {}
    for index, raw in enumerate(events):
        device_id = _field(raw, "device_id")
        entity_id = _field(raw, "entity_id")
        if not device_id or not entity_id:
            continue
        device = str(device_id)
        if not _is_load_change(raw, by_entity.get(str(entity_id))):
            continue
        buckets.setdefault(device, []).append(raw)
        first_seen.setdefault(device, index)

    groups: list[PhysicalDeviceEvent] = []
    epoch = datetime.min.replace(tzinfo=UTC)
    for device, raws in buckets.items():
        ordered = sorted(raws, key=lambda item: _aware(_field(item, "occurred_at", epoch)))
        states: dict[str, str] = {}
        previous_states: dict[str, str] = {}
        polarities: set[bool] = set()
        for raw in ordered:
            entity = str(_field(raw, "entity_id"))
            label = _state_label(raw)
            if label is not None:
                states[entity] = label
            previous = _field(raw, "previous_state")
            if previous is not None:
                previous_states[entity] = str(previous).strip().lower()
            polarity = _state_polarity(raw)
            if polarity is not None:
                polarities.add(polarity)
        notes: list[str] = []
        conflicting = len(polarities) > 1
        if conflicting:
            notes.append("conflicting_observations")
        if device in contributor_conflicts:
            notes.append("power_contributor_conflict")
        occurred = _aware(_field(ordered[0], "occurred_at", epoch))
        received = max(
            (_aware(_field(item, "received_at", occurred)) for item in ordered),
            default=occurred,
        )
        entity_ids = tuple(sorted(states)) or tuple(
            sorted({str(_field(item, "entity_id")) for item in ordered})
        )
        ct_ids = frozenset().union(*(_ct_ids(item) for item in ordered))
        groups.append(
            PhysicalDeviceEvent(
                device_id=device,
                occurred_at=occurred,
                received_at=received,
                event_ids=tuple(
                    _identity(item, device, _field(item, "occurred_at", epoch)) for item in ordered
                ),
                entity_ids=entity_ids,
                states=states,
                previous_states=previous_states,
                events=tuple(ordered),
                power_contributor=contributors.get(device),
                conflicting=conflicting,
                notes=tuple(notes),
                ct_ids=ct_ids,
            )
        )

    groups.sort(key=lambda group: (_aware(group.occurred_at), first_seen[group.device_id]))
    return groups


# --------------------------------------------------------------------------- #
# Automation event groups (spec 07 §9)
# --------------------------------------------------------------------------- #

#: How close two member changes must be to belong to one automation group.
#: Deliberately wider than ``correlation.CO_TOGGLE_S`` (5 s): that constant is a
#: *placement* gate for a history recorder, while this one decides whether two
#: simultaneous physical changes are evidence about one cause.  The spec's
#: membership rule is "declared by the HA automation's id plus observed time
#: proximity"; the proximity bound has to exist and be named, or an automation
#: that ramps its members over a minute would silently become N causes.
GROUP_PROXIMITY_S = 30.0


def _automation_key(obj: Any) -> str | None:
    """The declared automation/script id, when the event carries one.

    ``attributes["automation_id"]`` is what Home Assistant puts on an entity
    state change caused by an automation; ``script_id`` / ``cause_id`` /
    ``correlation_group`` are accepted as aliases so a producer that names the
    cause differently is not silently ungrouped.
    """
    attributes = _field(obj, "attributes")
    if isinstance(attributes, Mapping):
        for name in ("automation_id", "script_id", "cause_id", "correlation_group"):
            raw = attributes.get(name)
            if raw not in (None, ""):
                return str(raw)
    for name in ("automation_id", "correlation_group"):
        raw = _field(obj, name)
        if raw not in (None, ""):
            return str(raw)
    return None


@dataclass(frozen=True, slots=True)
class EventGroup:
    """One candidate grouping of events that share a cause.

    A group is deliberately a *transient correlation artefact*, not a persisted
    object: it is derived from the events in one neighbourhood and published
    with them.  ``kind`` is the spec's ``automation`` / ``simultaneous`` /
    ``manual`` vocabulary.  Membership never attributes anything -- it records
    that these changes were one cause, which is why a member of a group of size
    > 1 is never credited an independent step.
    """

    group_id: str
    member_event_ids: tuple[str, ...]
    device_ids: tuple[str, ...]
    kind: str
    #: The cause, when the events declared one (an HA automation id).
    cause: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    completeness: str = "complete"
    #: The raw members, carried so a caller can read their states and kinds
    #: without re-deriving the membership.  Never serialized by :meth:`as_dict`.
    members: tuple[Any, ...] = ()

    @property
    def size(self) -> int:
        return len(self.member_event_ids)

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "member_event_ids": list(self.member_event_ids),
            "device_ids": list(self.device_ids),
            "kind": self.kind,
            "cause": self.cause,
            "window_start": None
            if self.window_start is None
            else self.window_start.isoformat(),
            "window_end": None if self.window_end is None else self.window_end.isoformat(),
            "completeness": self.completeness,
            "size": self.size,
        }


def group_events(
    events: Iterable[Any],
    *,
    proximity_s: float = GROUP_PROXIMITY_S,
) -> list[EventGroup]:
    """Group one neighbourhood's events by declared cause and time proximity.

    Two rules, both from spec 07 §9.2:

    * a **declared** cause (the automation id on each member) groups its members
      regardless of spacing -- the declaration is the fact, proximity only
      corroborates it;
    * without a declaration, events whose ``occurred_at`` falls within
      ``proximity_s`` of the group's first member are one **candidate**
      simultaneity group.

    One event is returned in at most one group.  A singleton group is omitted:
    a group of one is not a group, and returning it would make every isolated
    event look grouped.  Events are ordered by ``occurred_at`` (never arrival),
    so the result is deterministic for a fixed input.
    """
    items = [item for item in events if _field(item, "device_id")]
    if not items:
        return []
    ordered = sorted(items, key=lambda item: (_aware(_field(item, "occurred_at")),))
    groups: list[EventGroup] = []

    # Pass 1 -- declared causes.
    by_cause: dict[str, list[Any]] = {}
    for item in ordered:
        cause = _automation_key(item)
        if cause is not None:
            by_cause.setdefault(cause, []).append(item)
    for cause in sorted(by_cause):
        members = by_cause[cause]
        if len(members) < 2:
            continue
        groups.append(_build_group(members, "automation", cause=cause))

    # Pass 2 -- undeclared proximity clusters among the remaining events.
    assigned = {
        id(item)
        for group in groups
        for item in (group.members or ())
    }
    rest = [item for item in ordered if id(item) not in assigned]
    cluster: list[Any] = []
    for item in [*rest, None]:
        if item is None:
            if len(cluster) >= 2:
                groups.append(_build_group(cluster, "simultaneous"))
            break
        if (
            cluster
            and (
                _aware(_field(item, "occurred_at"))
                - _aware(_field(cluster[-1], "occurred_at"))
            ).total_seconds()
            > proximity_s
        ):
            if len(cluster) >= 2:
                groups.append(_build_group(cluster, "simultaneous"))
            cluster = []
        cluster.append(item)

    groups.sort(key=lambda group: (group.window_start or datetime.min.replace(tzinfo=UTC)))
    return groups


def _build_group(members: Sequence[Any], kind: str, *, cause: str | None = None) -> EventGroup:
    """Assemble one group from already-collected members."""
    stamps = [_aware(_field(item, "occurred_at")) for item in members]
    device_ids = tuple(
        dict.fromkeys(
            str(_field(item, "device_id")) for item in members if _field(item, "device_id")
        )
    )
    prefix = cause or kind
    payload = "|".join(
        [
            str(prefix),
            min(stamps).isoformat(),
            *[str(device) for device in device_ids],
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return EventGroup(
        group_id=f"grp_{digest[:16]}",
        member_event_ids=tuple(
            _identity(item, str(_field(item, "device_id")), _field(item, "occurred_at"))
            for item in members
        ),
        device_ids=device_ids,
        kind=kind,
        cause=cause,
        window_start=min(stamps),
        window_end=max(stamps),
        members=tuple(members),
    )


# --------------------------------------------------------------------------- #
# Isolator
# --------------------------------------------------------------------------- #


class EventIsolator:
    """Decide whether one event's power window can be attributed to one device."""

    def __init__(
        self,
        config: IsolationConfig | None = None,
        *,
        bindings: Iterable[HaEntityBinding] = (),
        device_classes: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self.config = config or IsolationConfig()
        self.bindings = list(bindings)
        #: ``device_id -> class labels`` used for the middle config layer.
        self._device_classes = {
            str(device): tuple(str(label) for label in labels)
            for device, labels in (device_classes or {}).items()
        }

    # -- identity --------------------------------------------------------- #
    def physical_events(self, events: Iterable[Any]) -> list[PhysicalDeviceEvent]:
        """One :class:`PhysicalDeviceEvent` per physical device, time ordered."""
        return physical_device_events(events, self.bindings)

    def groups(self, events: Iterable[Any]) -> list[EventGroup]:
        """The candidate groups among ``events`` (spec 07 §9)."""
        return group_events(events)

    def window_for(self, device_id: str) -> tuple[IsolationWindow, str]:
        return self.config.resolve(device_id, self._device_classes.get(device_id, ()))

    def resolved_window_for(self, device_id: str) -> ResolvedWindow:
        """The window this device's verdicts are decided in, with its source."""
        return self.config.resolved_window(
            device_id, self._device_classes.get(device_id, ())
        )

    # -- classification --------------------------------------------------- #
    def classify(
        self,
        event: Any,
        competing_events: Iterable[Any],
        *,
        now: datetime | None = None,
        coverage: SampleCoverage | Mapping[str, Any] | None = None,
    ) -> EventIsolation:
        """Classify one event using the *other* events in its neighbourhood.

        ``competing_events`` may include ``event`` itself (the normal case when
        a caller passes a whole window); the subject is removed by physical
        device, never by identity, so a second entity of the same appliance can
        never masquerade as a competitor.
        """
        occurred_at = _field(event, "occurred_at")
        if not isinstance(occurred_at, datetime):
            raise TypeError("event.occurred_at must be a datetime")
        received_at = _field(event, "received_at") or occurred_at
        device_id = str(_field(event, "device_id", ""))
        entity_id = str(_field(event, "entity_id", ""))
        event_id = _identity(event, device_id, occurred_at)

        resolved = self.resolved_window_for(device_id)
        window = resolved.window
        source = resolved.source
        coverage_obj = _coerce_coverage(coverage)
        window_start, window_end, power_end = window.shifted(occurred_at)
        evidence: dict[str, Any] = {
            "config_source": source,
            #: How the *width* was obtained: ``observed_history`` when the
            #: global window was derived from the reader's own cadence,
            #: ``configured`` when it was read off the declared width.  This is
            #: a different question from ``config_source`` (which configuration
            #: layer answered), and it is not ``aligner.CADENCE_SOURCE_DEFAULT``
            #: (``"configured_default"``): that one labels a fallback *cadence*,
            #: this one labels a *window width*.
            "window_source": resolved.cadence_source,
            "resolved_window": resolved.as_dict(),
            "device_class": list(self._device_classes.get(device_id, ())),
            #: Every event names its kind (spec 07 §2), and the verdict carries
            #: it so a consumer never has to re-derive it from the event shape.
            "event_kind": event_kind(event),
            "event_kind_source": event_kind_source(event),
        }

        def build(
            status: IsolationStatus,
            reason: str,
            *,
            competing_event_ids: Sequence[str] = (),
            competing_device_ids: Sequence[str] = (),
            power_window_contaminated: bool = False,
            extra: Mapping[str, Any] | None = None,
        ) -> EventIsolation:
            coverage_payload: dict[str, Any] = {}
            if coverage_obj is not None:
                coverage_payload = {
                    **coverage_obj.as_dict(),
                    "sufficient": coverage_obj.sufficient(
                        min_baseline=self.config.min_baseline_samples,
                        min_event=self.config.min_event_samples,
                    ),
                }
            return EventIsolation(
                status=status,
                event_id=event_id,
                device_id=device_id,
                entity_id=entity_id,
                occurred_at=occurred_at,
                received_at=received_at,
                analysis_offset_s=window.analysis_offset_s,
                isolation_window_s=window.isolation_window_s,
                window_start=window_start,
                window_end=window_end,
                competing_event_ids=list(competing_event_ids),
                competing_device_ids=list(competing_device_ids),
                power_window_contaminated=power_window_contaminated,
                reason=reason,
                evidence={**evidence, "coverage": coverage_payload, **(extra or {})},
            )

        # 1. Naive timestamps cannot be placed on a timeline at all.
        if occurred_at.tzinfo is None:
            return build(IsolationStatus.INSUFFICIENT_DATA, REASON_NAIVE_TIME)

        # 2. The subject must itself be load evidence.  An availability flip of
        #    the device is a real event, but it says nothing about watts.
        if not _is_load_change(event, _bindings_by_entity(self.bindings).get(entity_id)):
            return build(IsolationStatus.INSUFFICIENT_DATA, REASON_NOT_LOAD_EVIDENCE)

        # 3. Collapse entities, then drop the subject's own physical device.
        groups = self.physical_events(competing_events)
        own_group = next((group for group in groups if group.device_id == device_id), None)
        others = [group for group in groups if group.device_id != device_id]
        if own_group is not None:
            evidence["own_device_conflicting"] = own_group.conflicting
            evidence["own_group"] = {
                "event_ids": list(own_group.event_ids),
                "entity_ids": list(own_group.entity_ids),
                "conflicting": own_group.conflicting,
                "power_contributor": own_group.power_contributor,
            }

        # Groups are computed over the *raw* neighbourhood, not the collapsed
        # physical devices: two HA entities of one appliance are one physical
        # change but are separate events, and a declared automation id lives on
        # the entity event.  A subject in a group of size > 1 is credited as a
        # member, never as an independent step.
        event_groups = group_events(competing_events)
        own_event_groups = [
            group for group in event_groups if event_id in set(group.member_event_ids)
        ]
        evidence["event_groups"] = [group.as_dict() for group in event_groups]
        evidence["own_event_groups"] = [group.group_id for group in own_event_groups]
        evidence["own_group_size"] = max(
            (len(group.member_event_ids) for group in own_event_groups), default=1
        )
        evidence["grouped_once"] = bool(own_event_groups)

        in_isolation = [
            group for group in others if window_start <= _aware(group.occurred_at) < window_end
        ]
        in_power = [group for group in others if window_end <= _aware(group.occurred_at) < power_end]
        competing = in_isolation or in_power
        competing_event_ids = [
            group.event_ids[0] for group in competing if group.event_ids and group.event_ids[0]
        ]
        competing_device_ids = list(dict.fromkeys(group.device_id for group in competing))

        # The verdict answers the spec's question literally: "when this device
        # changed, was anything else changing in the same window?"  Whether the
        # competitor could actually pollute *this sample's* delta is a separate,
        # narrower question that only the learning guard needs to ask, so it is
        # recorded here as audit evidence rather than applied to the status.
        # A consumer that has no CT model keeps the conservative reading.
        subject_cts = _ct_ids(event)
        separated = [group for group in competing if not could_share_ct(subject_cts, group)]
        evidence.update(
            {
                "competing_groups": [group.as_dict() for group in competing],
                "competing_device_count": len(competing_device_ids),
                "in_isolation_window": bool(in_isolation),
                "in_power_window": bool(in_power),
                "window_elapsed": (now if now is not None else utcnow()) >= window_end,
                "power_window_start": window_start.isoformat(),
                "power_window_end": power_end.isoformat(),
                "ct_scope_declared": bool(subject_cts),
                "own_ct_ids": sorted(subject_cts),
                #: Competitors on a disjoint CT: same window, different meter.
                "ct_separated_device_ids": list(
                    dict.fromkeys(group.device_id for group in separated)
                ),
            }
        )

        # 4. Contamination is positive evidence and needs no sample coverage.
        if in_isolation:
            return build(
                IsolationStatus.CONTAMINATED,
                REASON_CONCURRENT,
                competing_event_ids=competing_event_ids,
                competing_device_ids=competing_device_ids,
                power_window_contaminated=True,
            )
        if in_power:
            return build(
                IsolationStatus.CONTAMINATED,
                REASON_POWER_WINDOW,
                competing_event_ids=competing_event_ids,
                competing_device_ids=competing_device_ids,
                power_window_contaminated=True,
            )

        # 5. No contamination: now the samples must actually exist.
        if coverage_obj is None:
            return build(IsolationStatus.INSUFFICIENT_DATA, REASON_COVERAGE_UNKNOWN)
        if not coverage_obj.sufficient(
            min_baseline=self.config.min_baseline_samples,
            min_event=self.config.min_event_samples,
        ):
            return build(IsolationStatus.INSUFFICIENT_DATA, REASON_INSUFFICIENT_SAMPLES)

        # 6. A window nobody has observed yet cannot be declared clean.
        if not evidence["window_elapsed"]:
            return build(IsolationStatus.INSUFFICIENT_DATA, REASON_WINDOW_NOT_ELAPSED)

        return build(IsolationStatus.ISOLATED, REASON_ISOLATED)

    # -- late events ------------------------------------------------------- #
    def reclassify(self, previous: EventIsolation, late_event: Any) -> EventIsolation:
        """Retroactively contaminate ``previous`` if ``late_event`` overlaps.

        Returns ``previous`` unchanged (by identity) when the late event cannot
        affect it: another device already contaminated it, it belongs to the
        same physical device, it is not load evidence, or it falls outside both
        recorded windows.  ``occurred_at`` is copied, never recomputed.
        """
        if previous.status != IsolationStatus.ISOLATED:
            return previous
        occurred_at = _field(late_event, "occurred_at")
        if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
            return previous
        if str(_field(late_event, "device_id", "")) == previous.device_id:
            return previous
        entity_id = str(_field(late_event, "entity_id", ""))
        if not _is_load_change(late_event, _bindings_by_entity(self.bindings).get(entity_id)):
            return previous

        window_start = previous.window_start
        if window_start.tzinfo is None:  # pragma: no cover - stored as aware
            window_start = window_start.replace(tzinfo=UTC)
        evidence = dict(previous.evidence)
        wide_s = max(
            previous.isolation_window_s,
            float(evidence.get("power_window_s") or previous.isolation_window_s),
        )
        power_end = window_start + timedelta(seconds=wide_s)
        recorded = evidence.get("power_window_end")
        if isinstance(recorded, str):
            with contextlib.suppress(ValueError):  # pragma: no cover - defensive
                power_end = datetime.fromisoformat(recorded)
        if not (window_start <= occurred_at < power_end):
            return previous

        device_id = str(_field(late_event, "device_id", ""))
        event_id = _identity(late_event, device_id, occurred_at)
        event_ids = list(dict.fromkeys([*previous.competing_event_ids, event_id]))
        device_ids = list(dict.fromkeys([*previous.competing_device_ids, device_id]))
        status = previous.status
        return EventIsolation(
            status=IsolationStatus.CONTAMINATED,
            event_id=previous.event_id,
            device_id=previous.device_id,
            entity_id=previous.entity_id,
            occurred_at=previous.occurred_at,
            received_at=previous.received_at,
            analysis_offset_s=previous.analysis_offset_s,
            isolation_window_s=previous.isolation_window_s,
            window_start=previous.window_start,
            window_end=previous.window_end,
            competing_event_ids=event_ids,
            competing_device_ids=device_ids,
            power_window_contaminated=True,
            reason=REASON_LATE,
            evidence={
                **evidence,
                "reclassified": True,
                "previous_status": status.value if hasattr(status, "value") else str(status),
                "late_event_id": event_id,
                "late_device_id": device_id,
                "competing_device_count": len(device_ids),
            },
        )


# --------------------------------------------------------------------------- #
# Retention / clean-attribution filter
# --------------------------------------------------------------------------- #


def split_attribution(
    isolations: Iterable[EventIsolation],
) -> tuple[list[EventIsolation], list[EventIsolation]]:
    """Partition into ``(clean, withheld)`` without dropping anything.

    ``withheld`` keeps contaminated *and* insufficient-data events: the first
    because the power change had more than one cause, the second because it
    cannot be shown to have had one.
    """
    clean: list[EventIsolation] = []
    withheld: list[EventIsolation] = []
    for isolation in isolations:
        (clean if isolation.status == IsolationStatus.ISOLATED else withheld).append(isolation)
    return clean, withheld


def clean_attribution_inputs(isolations: Iterable[EventIsolation]) -> list[EventIsolation]:
    """The only exclusion point: events safe to feed clean attribution."""
    return split_attribution(isolations)[0]


__all__ = [
    "CADENCE_SOURCE_DEFAULT",
    "CADENCE_SOURCE_OBSERVED",
    "EVENT_KIND_HA",
    "EVENT_KIND_MEASUREMENT",
    "EVENT_KIND_UNKNOWN_RESIDUAL",
    "EVENT_KINDS",
    "EventGroup",
    "EventIsolator",
    "GROUP_PROXIMITY_S",
    "IsolationConfig",
    "IsolationWindow",
    "KIND_SOURCE_EXPLICIT",
    "KIND_SOURCE_EVENT_TYPE",
    "KIND_SOURCE_INFERRED",
    "MAX_DERIVED_WINDOW_S",
    "PhysicalDeviceEvent",
    "ResolvedWindow",
    "SampleCoverage",
    "UNKNOWN_RESIDUAL_MARKER",
    "WINDOW_CADENCES",
    "WINDOW_SOURCE_CONFIGURED",
    "WINDOW_SOURCE_OBSERVED",
    "clean_attribution_inputs",
    "could_share_ct",
    "event_kind",
    "event_kind_source",
    "group_events",
    "physical_device_events",
    "split_attribution",
    "window_from_cadence",
]
