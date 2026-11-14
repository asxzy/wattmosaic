"""Device→CT association as an evidence-bearing, withdrawable relation.

The gap this closes
-------------------
Before this module a device's association to a CT lived in three unrelated
fields (``CTChannel.device_id``, ``DeviceTopology.dedicated_ct_ids``,
``DeviceConfiguration.monitored_by_ct_ids``).  None of them carried evidence,
none carried a validity interval, and the last write won.  "Which CT observes
this device?" was therefore unanswerable after the fact — you could see the
current claim but not why it was made, how strong it was, or when it stopped
being true.

This module answers it with one typed edge,
:class:`~wattmosaic_engine.contracts.TopologyRelation` of kind
``DEVICE_CT_ASSOCIATION``, whose ``evidence`` dict *is* the argument:

``evidence["stats"]``
    the numbers the decision was made from (event count, median/MAD deltas,
    lag, stability, measurement basis and multiplier);
``evidence["supporting"]``
    every clean isolated event that agreed, one entry each;
``evidence["conflicting"]``
    every clean isolated event that disagreed, with the reason -- never
    discarded, never averaged into the support;
``evidence["overlap"]``
    how this CT relates to the *other* CTs that also see the same device
    (a duplicate conductor, a factor-adjusted appliance total, or genuinely
    independent contributions), so equal deltas can never be read as two
    independent loads;
``evidence["verdict"]``
    ``supported`` / ``split`` / ``ambiguous`` / ``single_event``.

What each evidence rule enforces, and why
----------------------------------------
1. **Only isolated events are evidence.**  A contaminated event means "more
   than one load changed in this window", so no amount of arithmetic can turn
   it into clean support.  It is counted as ``contaminated_event`` and
   excluded, exactly as ``clean_attribution_inputs`` prescribes; nothing is
   deleted.
2. **Magnitude consistency.**  The device's own declared step and the CT's
   measured step must agree in *sign* and in *size*: the CT may carry a share
   of the device (a leg, a branch) but never more than the device plus
   :attr:`AssociationConfig.magnitude_tolerance_frac`.  A CT that moves four
   times as much is a different load that happens to move at the same time —
   the "correlation alone is insufficient" case.
3. **Repeatability.**  A relation needs ``min_events`` independent isolated
   events.  One coincidence cannot be told from one real event, so a candidate
   built on a single event is capped at :data:`SINGLE_EVENT_CAP` confidence and
   labelled ``single_event`` even when the caller lowers ``min_events``.
4. **Lag.**  A CT that publishes every 15 s and a device that switches now
   cannot be required to agree at the same instant.  The response is searched
   over ``0..max_lag_s`` and the best lag is reported in seconds.  Late is not
   wrong.
5. **Noise.**  Median and MAD, never mean and standard deviation.  One 5 kW
   spike among five 200 W events moves the mean by 960 W and the median by 0 W;
   the spike is kept as a conflicting observation instead of being laundered
   into the estimate.
6. **Basis compatibility.**  A factor-adjusted CT's delta is *converted with
   the recorded multiplier* before comparison, and the multiplier and basis are
   written into the evidence.  An ``UNKNOWN`` basis is refused outright: with
   unestablished semantics, multiplying and not multiplying both guess.  Two
   CTs whose bases say "one of us already estimates the whole appliance" are
   reported as an overlap and ``summing_allowed=False``.
7. **Equal deltas prove overlap, not loads.**  Two CTs that both move by the
   device's whole step (or that report the same watts) are measurements of one
   conductor / one appliance.  Both associations are emitted — both CTs really
   do observe the device — and the pair is marked as not summable.  No
   independent device load is invented.
8. **Ambiguity is named.**  ``TopologyStatus`` has no ``AMBIGUOUS`` member
   (only ``CANDIDATE`` / ``CONFIRMED`` / ``REJECTED``), so ambiguity is
   encoded in ``evidence["verdict"] = "ambiguous"`` / ``"split"`` with the
   competing device ids, and confidence is multiplied by
   :data:`AMBIGUOUS_MULTIPLIER` / :data:`SPLIT_MULTIPLIER`.  The relation stays
   ``CANDIDATE`` and never carries an independent power estimate.

The same rule covers events that *cannot* be attributed: when several devices
move in one instant, the CT's delta is their sum, so it is recorded under
``evidence["split_with"]`` and ``stats["median_observed_delta_w"]`` — a real
measurement of the conductor, never this device's share.  ``stats["n_events"]``
counts every event the pair was judged on (supporting + conflicting + joint);
``stats["n_supporting"]`` is the subset an individual device could be held to.

Confidence
----------
``confidence`` is a *CT-association* confidence, not a device power score, and
it is computed from four independently observable terms::

    repeatability = 1 - 0.5 ** n_events          # 1 -> 0.50, 4 -> 0.94, 8 -> 0.996
    stability     = clip(1 - MAD(contribution) / median(contribution), 0, 1)
    basis_term    = min(registry confidence factor over the support)
    lag_term      = 1 - 0.5 * (lag_s / max_lag_s)   # 0 s -> 1.0, 60 s -> 0.5
    ambiguity     = 1.0 (supported) / 0.75 (split) / 0.25 (ambiguous)

    confidence = min(MAX_CONFIDENCE,
                     repeatability * stability * basis_term * lag_term * ambiguity
                     / (1 + conflict_penalty * n_conflicts))

So ``0.95`` is reproducible and readable: it means *"five clean, isolated
events, all agreeing to within about 2 % of the same share, on a CT whose
measurement basis is established, responding by the same instant, with nothing
contradicting them"* -- ``0.9688 * 0.98 = 0.949``.  A reader can recompute every
factor of it from ``evidence["stats"]``; nothing in the number is a hidden
constant.  It is *never* 1.0: :data:`MAX_CONFIDENCE` is 0.99, because a candidate
association is a proposal and confirmation is the user's act.

Orientation
-----------
A CT clamped backwards reports ``-delta`` where the device reports ``+delta``.
This module does **not** silently take an absolute value: the reversed step is
withheld with ``opposite_direction``.  Orientation is accounted for upstream —
by the declared ``Polarity.INVERTED`` on the channel, passed to :meth:`observe`
as ``polarity`` (applied exactly once) — and only then does the relation
support.  Silently flipping the sign would hide a real wiring fault and would
make "the device turned on and the CT fell" indistinguishable from a
neighbouring load switching off.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from wattmosaic_engine.contracts import (
    IsolationStatus,
    MeasurementBasis,
    RelationKind,
    TopologyRelation,
    TopologyStatus,
    utcnow,
)
from wattmosaic_engine.electrical.factors import FactorRegistry

#: ``1.4826 * MAD`` is the normal-consistent scale of the median absolute
#: deviation; the constant only fixes the unit, not any threshold.
MAD_TO_SIGMA = 1.4826

#: A candidate is never presented as certain.  Confirmation is a user act, so
#: even overwhelming measurement evidence stops here.
MAX_CONFIDENCE = 0.99

#: Cap applied when the support is a single event (see ``verdict``).
SINGLE_EVENT_CAP = 0.5

#: Multipliers applied to the confidence when the evidence cannot single out
#: the device.  ``split`` means solo events exist and the joint response can be
#: apportioned; ``ambiguous`` means they never do, so the relation is a proposal
#: that must not be used as an independent power estimate.
SPLIT_MULTIPLIER = 0.75
AMBIGUOUS_MULTIPLIER = 0.25

#: A CT response must clear this many of its own baseline sigmas (MAD) to count
#: as a response at all.  3 is the ordinary "a real step, not jitter" floor and
#: is expressed in the channel's own units, so a quiet channel does not turn a
#: 1 W flicker into an association.
NOISE_SIGMAS = 3.0

#: Bases whose delta already claims the *whole appliance*, not one conductor.
APPLIANCE_TOTAL_BASES = frozenset(
    {MeasurementBasis.FACTOR_ADJUSTED.value, MeasurementBasis.MONITOR_REPORTED.value}
)

VERDICT_SUPPORTED = "supported"
VERDICT_SPLIT = "split"
VERDICT_AMBIGUOUS = "ambiguous"
VERDICT_SINGLE_EVENT = "single_event"

# Withheld-reason codes.  Stable machine strings, so a report can be explained.
REASON_CONTAMINATED = "contaminated_event"
REASON_NO_CT_EVIDENCE = "no_ct_evidence"
REASON_TOO_FEW_EVENTS = "too_few_events"
REASON_BASIS_UNESTABLISHED = "basis_unestablished"
REASON_OPPOSITE_DIRECTION = "opposite_direction"
REASON_MAGNITUDE_OVERSHOOT = "magnitude_overshoot"
REASON_RESPONSE_WITHIN_NOISE = "response_within_noise"
REASON_LAG_EXCEEDED = "lag_exceeded"
REASON_INSUFFICIENT_MAGNITUDE = "insufficient_magnitude"
REASON_UNSTABLE = "unstable"
REASON_BUDGET_EXCEEDED = "budget_exceeded"

OVERLAP_DUPLICATE = "duplicate_measurement_of_one_device"
OVERLAP_APPLIANCE_TOTAL = "appliance_total_estimate_overlaps_sibling"
OVERLAP_UNATTRIBUTED = "contribution_never_isolated"
OVERLAP_INDEPENDENT = "independent_contributions"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _assertion_instant(now: datetime | None, valid_from: datetime | None) -> datetime:
    """When the assertion was *made*, with no hidden wall clock in analysis.

    ``now`` is the injected analysis instant, so a replay stamps the same
    ``created_at`` and two seeded runs are byte-identical.  Falling back to
    ``valid_from`` (the earliest supporting event) and only then to the wall
    clock keeps a relation honest even when a caller omits ``now``: the pure
    analysis path never reads the clock on its own.
    """
    if now is not None:
        return _aware(now)
    if valid_from is not None:
        return _aware(valid_from)
    return utcnow()


def _value(item: Any) -> str:
    return str(getattr(item, "value", item))


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def _mad_w(values: Sequence[float]) -> float:
    """Normal-consistent median absolute deviation, in watts."""
    if not values:
        return 0.0
    centre = _median(values)
    return MAD_TO_SIGMA * _median([abs(value - centre) for value in values])


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AssociationConfig:
    """Every threshold the association decision depends on, in one place.

    ``min_events``
        Independent isolated events required before a relation may be emitted.
        Two is the minimum that can distinguish repeatability from coincidence;
        ``1`` is permitted for diagnostics only and caps confidence at
        :data:`SINGLE_EVENT_CAP` with verdict ``single_event``.
    ``min_magnitude_w``
        Absolute floor (W) for both the device step and the CT step.  Below it
        the step is indistinguishable from measurement resolution and is not
        evidence.
    ``max_lag_s``
        Largest device→CT response delay searched, in seconds.  A response
        later than this is withheld (``lag_exceeded``) rather than ignored:
        a CT that reports a minute late is a different measurement, not the
        same one.
    ``magnitude_tolerance_frac``
        Fractional tolerance on ``ct_delta / device_delta``.  A CT may carry
        ``0 .. 1 + tolerance`` of the device step.  More than that cannot be
        the same load.
    ``stability_min``
        Minimum ``1 - MAD(contribution) / median(contribution)``.  Below it the
        CT responds inconsistently to repeated events and the relation is
        withheld as ``unstable``.
    ``conflict_penalty``
        Confidence divisor per conflicting observation:
        ``1 + conflict_penalty * n_conflicts``.  Contradicting evidence
        therefore lowers confidence monotonically without ever erasing the
        support that was observed.
    ``algorithm_version``
        Travels onto every emitted relation so a stored candidate can be
        explained after an upgrade.
    ``noise_sigmas``
        Response significance in units of the CT's own baseline MAD.
    ``lag_step_s``
        Resolution of the lag search, in seconds.  Defaults to the audited CT
        update cadence so a step is never placed between two samples.
    """

    min_events: int = 2
    min_magnitude_w: float = 10.0
    max_lag_s: float = 60.0
    magnitude_tolerance_frac: float = 0.10
    stability_min: float = 0.70
    conflict_penalty: float = 0.15
    algorithm_version: str = "assoc@1.0"
    noise_sigmas: float = NOISE_SIGMAS
    lag_step_s: float = 15.0

    def __post_init__(self) -> None:
        if int(self.min_events) < 1:
            raise ValueError("min_events must be >= 1 (1 means diagnostic only)")
        if not math.isfinite(float(self.min_magnitude_w)) or float(self.min_magnitude_w) < 0.0:
            raise ValueError("min_magnitude_w must be finite and non-negative")
        if not math.isfinite(float(self.max_lag_s)) or float(self.max_lag_s) < 0.0:
            raise ValueError("max_lag_s must be finite and non-negative")
        tolerance = float(self.magnitude_tolerance_frac)
        if not math.isfinite(tolerance) or not 0.0 <= tolerance < 1.0:
            raise ValueError("magnitude_tolerance_frac must be finite and in [0, 1)")
        stability = float(self.stability_min)
        if not math.isfinite(stability) or not 0.0 < stability <= 1.0:
            raise ValueError("stability_min must be finite and in (0, 1]")
        penalty = float(self.conflict_penalty)
        if not math.isfinite(penalty) or penalty < 0.0:
            raise ValueError("conflict_penalty must be finite and non-negative")
        if not math.isfinite(float(self.noise_sigmas)) or float(self.noise_sigmas) <= 0.0:
            raise ValueError("noise_sigmas must be finite and positive")
        if not math.isfinite(float(self.lag_step_s)) or float(self.lag_step_s) <= 0.0:
            raise ValueError("lag_step_s must be finite and positive")
        object.__setattr__(self, "min_events", int(self.min_events))
        object.__setattr__(self, "min_magnitude_w", float(self.min_magnitude_w))
        object.__setattr__(self, "max_lag_s", float(self.max_lag_s))
        object.__setattr__(self, "magnitude_tolerance_frac", tolerance)
        object.__setattr__(self, "stability_min", stability)
        object.__setattr__(self, "conflict_penalty", penalty)
        object.__setattr__(self, "noise_sigmas", float(self.noise_sigmas))
        object.__setattr__(self, "lag_step_s", float(self.lag_step_s))

    def as_metadata(self) -> dict[str, Any]:
        return {
            "min_events": self.min_events,
            "min_magnitude_w": self.min_magnitude_w,
            "max_lag_s": self.max_lag_s,
            "magnitude_tolerance_frac": self.magnitude_tolerance_frac,
            "stability_min": self.stability_min,
            "conflict_penalty": self.conflict_penalty,
            "algorithm_version": self.algorithm_version,
            "noise_sigmas": self.noise_sigmas,
            "lag_step_s": self.lag_step_s,
        }


# --------------------------------------------------------------------------- #
# Observation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Observation:
    """One device event × one CT, measured from the windows around the event.

    ``ct_id is None`` is a deliberate, load-bearing value: it means "the device
    event was observed but no CT sample was available in its windows".  That is
    *evidence of absence of measurement*, which is reported as
    ``no_ct_evidence``; it is never treated as a CT response of zero watts.
    """

    event_id: str
    device_id: str
    ct_id: str | None
    occurred_at: datetime
    device_delta_w: float | None
    ct_delta_w: float | None
    lag_s: float = 0.0
    ct_noise_w: float = 0.0
    ct_samples: int = 0
    ct_level_w: float | None = None
    isolation_status: str = IsolationStatus.ISOLATED.value
    reason: str = ""
    orientation: str = "normal"

    @property
    def isolated(self) -> bool:
        return self.isolation_status == IsolationStatus.ISOLATED.value

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "device_id": self.device_id,
            "ct_id": self.ct_id,
            "occurred_at": _aware(self.occurred_at).isoformat(),
            "device_delta_w": None if self.device_delta_w is None else round(self.device_delta_w, 6),
            "ct_delta_w": None if self.ct_delta_w is None else round(self.ct_delta_w, 6),
            "lag_s": round(self.lag_s, 3),
            "ct_noise_w": round(self.ct_noise_w, 6),
            "ct_samples": self.ct_samples,
            "ct_level_w": None if self.ct_level_w is None else round(self.ct_level_w, 6),
            "isolation_status": self.isolation_status,
            "orientation": self.orientation,
        }


# --------------------------------------------------------------------------- #
# Window arithmetic
# --------------------------------------------------------------------------- #


def _stamp(value: Any) -> datetime | None:
    """Coerce an observable timestamp to an aware ``datetime``.

    Observation views and the REST payloads that carry them serialise
    timestamps as ISO-8601 strings, so a string is a first-class input here,
    not a caller error.  Anything that is neither is skipped rather than
    guessed at.
    """
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str):
        try:
            return _aware(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def _group_samples(samples: Iterable[Any]) -> dict[str | None, list[tuple[datetime, float]]]:
    """``ct_id -> [(time, watts)]``; rows without ``ct_id`` are device samples."""
    out: dict[str | None, list[tuple[datetime, float]]] = defaultdict(list)
    for row in samples:
        stamp = _stamp(_field(row, "state_timestamp", _field(row, "timestamp")))
        if stamp is None:
            continue
        raw = _field(row, "value_w", _field(row, "value"))
        if raw is None:
            continue
        try:
            watts = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(watts):
            continue
        key = _field(row, "ct_id")
        out[None if key in (None, "") else str(key)].append((stamp, watts))
    for rows in out.values():
        rows.sort(key=lambda item: item[0])
    return dict(out)


def _best_lag(
    after: Sequence[tuple[datetime, float]],
    occurred_at: datetime,
    baseline: float,
    delta: float,
    *,
    max_lag_s: float,
    resolution_s: float = 0.0,
) -> float:
    """Seconds until the CT has moved half of its eventual step.

    Half the step is the natural, noise-tolerant crossing point: it is reached
    once per event (not at every sample above the threshold) and it does not
    depend on the final plateau, so a *delayed* response reports its delay
    rather than its settling time.

    A crossing that lands *before* the event cannot be a response to it (the CT
    was already there).  Such a reading is not clipped to zero, because zero is
    a claim of instant response; it is reported as ``max_lag_s``, which the
    classifier withholds as ``lag_exceeded``.  The same holds when no sample in
    the window ever crosses.

    ``resolution_s`` is the CT's audited publish cadence: a response cannot be
    timed more finely than the samples allow, so the answer is rounded down to
    that grid.  Rounding *down* is deliberate -- it never invents a delay that
    the timestamps do not show.
    """
    if delta == 0.0:
        return 0.0
    target = baseline + 0.5 * delta
    for stamp, watts in after:
        crossed = watts >= target if delta > 0 else watts <= target
        if crossed:
            lag = (stamp - _aware(occurred_at)).total_seconds()
            if lag < 0.0 or (max_lag_s > 0.0 and lag >= max_lag_s):
                break
            if resolution_s > 0.0:
                lag = math.floor(lag / resolution_s) * resolution_s
            return float(lag)
    # No usable crossing: the sentinel is ``max_lag_s``, and the classifier
    # withholds ``>= max_lag_s``.  The boundary value is deliberately *not*
    # accepted -- at the limit a real response and silence are indistinguishable,
    # and inventing support from that ambiguity is the failure this module exists
    # to prevent.
    return float(max_lag_s)


@dataclass(frozen=True, slots=True)
class _Response:
    delta_w: float
    noise_w: float
    lag_s: float
    samples: int
    level_w: float


def _response(
    before: Sequence[tuple[datetime, float]],
    after: Sequence[tuple[datetime, float]],
    occurred_at: datetime,
    *,
    max_lag_s: float,
    resolution_s: float = 0.0,
) -> _Response | None:
    if not before or not after:
        return None
    baseline = _median([value for _, value in before])
    settled = _median([value for _, value in after])
    delta = settled - baseline
    noise = _mad_w([value for _, value in before])
    return _Response(
        delta_w=delta,
        noise_w=noise,
        lag_s=_best_lag(
            after,
            occurred_at,
            baseline,
            delta,
            max_lag_s=max_lag_s,
            resolution_s=resolution_s,
        ),
        samples=len(before) + len(after),
        level_w=settled,
    )


# --------------------------------------------------------------------------- #
# Finder
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Candidate:
    """Mutable working state for one (device, CT) pair.

    ``supporting`` and ``joint`` are deliberately separate: a *joint* event is
    one where several devices moved at the same instant, so the CT response
    cannot be attributed to this device alone.  Joint events are therefore
    evidence *about the pair* (they prove the CT sees this device) but never
    evidence of the device's own share, and they never enter
    ``contributions`` -- the ratio that decides whether the CT carries a
    plausible fraction of the device step.
    """

    device_id: str
    ct_id: str
    supporting: list[dict[str, Any]] = field(default_factory=list)
    joint: list[dict[str, Any]] = field(default_factory=list)
    contributions: list[float] = field(default_factory=list)
    joint_ratios: list[float] = field(default_factory=list)
    deltas: list[float] = field(default_factory=list)
    raw_deltas: list[float] = field(default_factory=list)
    device_deltas: list[float] = field(default_factory=list)
    lags: list[float] = field(default_factory=list)
    noises: list[float] = field(default_factory=list)
    levels: list[float] = field(default_factory=list)
    basis: str = MeasurementBasis.CT_MEASURED.value
    applied_multiplier: float = 1.0
    basis_confidence: float = 1.0

    @property
    def contribution(self) -> float:
        return _median(self.contributions) if self.contributions else 0.0

    @property
    def n_observed(self) -> int:
        """Supporting + joint events; conflicts are added by the finder."""
        return len(self.supporting) + len(self.joint)

    def dispersion_source(self) -> list[float]:
        """The per-event ratios stability is measured on.

        Support gives the attributed share; a pair known *only* from joint
        events can still be tested for consistency, using the unattributed
        ratio, which is why the two are separate lists.
        """
        return self.contributions or self.joint_ratios


class AssociationFinder:
    """Accumulate isolated events into device→CT association candidates."""

    def __init__(self, config: AssociationConfig | None = None) -> None:
        self.config = config or AssociationConfig()
        self._last: tuple[list[Observation], FactorRegistry, datetime | None] | None = None
        self._withheld: dict[str, Counter[str]] = {}

    # ------------------------------------------------------------------ #
    # 1. observe
    # ------------------------------------------------------------------ #
    def observe(
        self,
        event_isolation: Any,
        before_window: Iterable[Any],
        after_window: Iterable[Any],
        samples: Iterable[Any] = (),
        *,
        device_delta_w: float | None = None,
        polarity: Mapping[str, Any] | None = None,
    ) -> list[Observation]:
        """Turn one classified event into zero or more CT observations.

        ``event_isolation`` is the :class:`~wattmosaic_engine.contracts.EventIsolation`
        produced by WP4.  ``INSUFFICIENT_DATA`` yields nothing at all: a window
        that could not be analysed is not evidence of a small response.  A
        ``CONTAMINATED`` verdict is retained (as ``conflicting``/withheld
        evidence) because it is a real observation of the device, it simply
        cannot support a clean association.

        ``device_delta_w`` is the device's own declared power step.  When it is
        omitted it is read from ``samples`` (device rows, i.e. rows without a
        ``ct_id``) so a caller that has measurement history need not restate it.

        ``polarity`` maps ``ct_id -> Polarity`` for channels whose orientation is
        *declared*.  An inverted CT is corrected exactly once here; an
        undeclared reversed reading stays reversed and is withheld upstream.
        """
        occurred_at = _field(event_isolation, "occurred_at")
        if not isinstance(occurred_at, datetime):
            raise TypeError("event_isolation.occurred_at must be a datetime")
        occurred_at = _aware(occurred_at)
        status = IsolationStatus(_value(_field(event_isolation, "status", IsolationStatus.ISOLATED)))
        if status is IsolationStatus.INSUFFICIENT_DATA:
            return []

        device_id = str(_field(event_isolation, "device_id", ""))
        event_id = str(_field(event_isolation, "event_id", "")) or f"{device_id}@{occurred_at.isoformat()}"

        grouped_before = _group_samples(before_window)
        grouped_after = _group_samples(after_window)
        if device_delta_w is None:
            device_delta_w = self._device_delta(grouped_before.get(None), grouped_after.get(None))
        delta = None if device_delta_w is None else float(device_delta_w)

        declared = {str(key): _value(item) for key, item in (polarity or {}).items()}
        observations: list[Observation] = []
        ct_ids = sorted({ct for ct in (*grouped_before, *grouped_after) if ct is not None})
        for ct_id in ct_ids:
            before = grouped_before.get(ct_id, [])
            after = grouped_after.get(ct_id, [])
            if declared.get(ct_id, "normal") == "inverted":
                before = [(stamp, -watts) for stamp, watts in before]
                after = [(stamp, -watts) for stamp, watts in after]
            response = _response(
                before,
                after,
                occurred_at,
                max_lag_s=self.config.max_lag_s,
                resolution_s=self.config.lag_step_s,
            )
            if response is None:
                continue
            observations.append(
                Observation(
                    event_id=event_id,
                    device_id=device_id,
                    ct_id=ct_id,
                    occurred_at=occurred_at,
                    device_delta_w=delta,
                    ct_delta_w=response.delta_w,
                    lag_s=response.lag_s,
                    ct_noise_w=response.noise_w,
                    ct_samples=response.samples,
                    ct_level_w=response.level_w,
                    isolation_status=status.value,
                    reason=str(_field(event_isolation, "reason", "")),
                    orientation="inverted_declared"
                    if declared.get(ct_id, "normal") == "inverted"
                    else "normal",
                )
            )

        if not observations:
            # The device moved but no CT measured it.  Recorded so the device is
            # reported as unobserved rather than silently absent.
            observations.append(
                Observation(
                    event_id=event_id,
                    device_id=device_id,
                    ct_id=None,
                    occurred_at=occurred_at,
                    device_delta_w=delta,
                    ct_delta_w=None,
                    isolation_status=status.value,
                    reason=str(_field(event_isolation, "reason", "")),
                )
            )
        return observations

    def _device_delta(
        self,
        before: Sequence[tuple[datetime, float]] | None,
        after: Sequence[tuple[datetime, float]] | None,
    ) -> float | None:
        if not before or not after:
            return None
        return _median([value for _, value in after]) - _median([value for _, value in before])

    # ------------------------------------------------------------------ #
    # 2. analyze
    # ------------------------------------------------------------------ #
    def analyze(
        self,
        observations: Iterable[Observation],
        *,
        registry: FactorRegistry | None = None,
        now: datetime | None = None,
    ) -> list[TopologyRelation]:
        """Emit one ``DEVICE_CT_ASSOCIATION`` candidate per supported pair."""
        rows = list(observations)
        factor_registry = registry if registry is not None else FactorRegistry()
        self._last = (rows, factor_registry, now)
        withheld: dict[str, Counter[str]] = defaultdict(Counter)

        active = self._partition(rows, withheld, factor_registry)
        joints = self._joint_events(active)

        candidates: dict[tuple[str, str], _Candidate] = {}
        conflicts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        pair_events: Counter[tuple[str, str]] = Counter()
        for row in active:
            assert row.ct_id is not None
            key = (row.device_id, row.ct_id)
            joint = len(joints.get((row.ct_id, row.occurred_at), ())) > 1
            classification, payload = self._classify(row, factor_registry, joint=joint)
            if classification == "conflict":
                conflicts[row.device_id].append(payload)
                pair_events[key] += 1
                withheld[row.device_id][str(payload["reason"])] += 1
                continue
            if classification != "support":
                continue
            pair_events[key] += 1
            candidate = candidates.setdefault(key, _Candidate(row.device_id, row.ct_id))
            if joint:
                self._add_joint(candidate, row, payload, joints)
            else:
                self._add_support(candidate, row, payload)

        self._withheld = {device: Counter(counts) for device, counts in withheld.items()}

        relations: list[TopologyRelation] = []
        per_device: dict[str, list[_Candidate]] = defaultdict(list)
        for candidate in candidates.values():
            per_device[candidate.device_id].append(candidate)

        for device_id, device_candidates in per_device.items():
            emitted = self._emit_device(
                device_id,
                device_candidates,
                conflicts.get(device_id, []),
                pair_events,
                now=now,
            )
            if emitted is None:
                continue
            relations.extend(emitted)

        relations.sort(key=lambda relation: (relation.subject_id, relation.object_id))
        return relations

    # -- partitioning --------------------------------------------------- #
    def _partition(
        self,
        rows: Sequence[Observation],
        withheld: dict[str, Counter[str]],
        registry: FactorRegistry,
    ) -> list[Observation]:
        """Drop non-evidence, counting *why*; return the analysable rows."""
        active: list[Observation] = []
        blind: Counter[str] = Counter()
        for row in rows:
            if not row.isolated:
                withheld[row.device_id][REASON_CONTAMINATED] += 1
                continue
            if row.ct_id is None or row.ct_delta_w is None:
                blind[row.device_id] += 1
                continue
            if row.device_delta_w is None or abs(row.device_delta_w) < self.config.min_magnitude_w:
                withheld[row.device_id][REASON_INSUFFICIENT_MAGNITUDE] += 1
                continue
            basis, _multiplier, _confidence = registry.basis_for(row.ct_id, row.occurred_at)
            if MeasurementBasis(_value(basis)) is MeasurementBasis.UNKNOWN:
                withheld[row.device_id][REASON_BASIS_UNESTABLISHED] += 1
                continue
            active.append(row)
        # "No CT saw this device" is a decision about the device, not a count of
        # its events: four blind events and one blind event both mean the same
        # thing, so it is recorded once.
        for device in blind:
            withheld[device][REASON_NO_CT_EVIDENCE] += 1
        return active

    # -- per-observation classification --------------------------------- #
    def _classify(
        self, row: Observation, registry: FactorRegistry, *, joint: bool = False
    ) -> tuple[str, dict[str, Any]]:
        """Classify one observation as support, conflict, or undecidable.

        ``joint`` relaxes **only** the magnitude bounds.  When several devices
        switch in the same instant the CT delta is their *sum*, so requiring it
        to be at most one device's step would reject every genuine shared
        conductor.  Sign, significance and lag are never relaxed: those are
        properties of the CT, not of the number of devices involved.
        """
        assert row.ct_id is not None and row.ct_delta_w is not None
        assert row.device_delta_w is not None
        basis, multiplier, basis_confidence = registry.basis_for(row.ct_id, row.occurred_at)
        scaled = row.ct_delta_w * float(multiplier)
        payload = {
            "event_id": row.event_id,
            "occurred_at": row.occurred_at.isoformat(),
            "ct_id": row.ct_id,
            "ct_delta_w": round(row.ct_delta_w, 6),
            "scaled_delta_w": round(scaled, 6),
            "device_delta_w": round(row.device_delta_w, 6),
            "lag_s": round(row.lag_s, 3),
            "basis": _value(basis),
            "applied_multiplier": float(multiplier),
            "basis_confidence": float(basis_confidence),
            "joint": joint,
        }
        if self.config.max_lag_s > 0.0 and row.lag_s >= self.config.max_lag_s:
            return "conflict", {**payload, "reason": REASON_LAG_EXCEEDED}
        if row.device_delta_w * scaled <= 0.0:
            return "conflict", {**payload, "reason": REASON_OPPOSITE_DIRECTION}
        significance = max(
            self.config.min_magnitude_w, self.config.noise_sigmas * row.ct_noise_w
        )
        if abs(scaled) < significance:
            return "conflict", {**payload, "reason": REASON_RESPONSE_WITHIN_NOISE}
        ratio = scaled / row.device_delta_w
        payload["ratio"] = ratio
        if not joint:
            # Only an *attributed* event can be judged against one device's
            # step.  A joint delta is a sum, so both bounds are meaningless.
            payload["contribution"] = ratio
            if ratio > 1.0 + self.config.magnitude_tolerance_frac:
                return "conflict", {**payload, "reason": REASON_MAGNITUDE_OVERSHOOT}
        if ratio <= 0.0:
            return "conflict", {**payload, "reason": REASON_OPPOSITE_DIRECTION}
        return "support", payload

    def _note_basis(self, candidate: _Candidate, payload: dict[str, Any]) -> None:
        """Record the basis facts of an accepted event (the weakest one wins)."""
        candidate.basis = str(payload["basis"])
        candidate.applied_multiplier = float(payload["applied_multiplier"])
        candidate.basis_confidence = min(
            candidate.basis_confidence, float(payload["basis_confidence"])
        )

    def _add_support(
        self, candidate: _Candidate, row: Observation, payload: dict[str, Any]
    ) -> None:
        """An isolated event attributable to exactly one device."""
        candidate.supporting.append(payload)
        candidate.contributions.append(float(payload["contribution"]))
        candidate.deltas.append(float(payload["scaled_delta_w"]))
        candidate.raw_deltas.append(float(payload["ct_delta_w"]))
        candidate.device_deltas.append(float(payload["device_delta_w"]))
        candidate.lags.append(float(payload["lag_s"]))
        candidate.noises.append(float(row.ct_noise_w))
        if row.ct_level_w is not None:
            candidate.levels.append(float(row.ct_level_w))
        self._note_basis(candidate, payload)

    def _add_joint(
        self,
        candidate: _Candidate,
        row: Observation,
        payload: dict[str, Any],
        joints: Mapping[tuple[str, datetime], set[str]],
    ) -> None:
        """Record a co-occurring event: it proves observation, not the share."""
        devices = sorted(str(item) for item in joints.get((candidate.ct_id, row.occurred_at), ()))
        rival = [device for device in devices if device != candidate.device_id]
        candidate.joint.append(
            {**payload, "device_ids": devices, "competing_devices": rival}
        )
        candidate.joint_ratios.append(float(payload["ratio"]))
        self._note_basis(candidate, payload)

    # -- joint (co-occurring) device events ------------------------------ #
    @staticmethod
    def _joint_events(rows: Sequence[Observation]) -> dict[tuple[str, datetime], set[str]]:
        out: dict[tuple[str, datetime], set[str]] = defaultdict(set)
        for row in rows:
            if row.ct_id is None:
                continue
            out[(row.ct_id, row.occurred_at)].add(row.device_id)
        return dict(out)

    # -- emission -------------------------------------------------------- #
    def _emit_device(
        self,
        device_id: str,
        candidates: list[_Candidate],
        conflicts: list[dict[str, Any]],
        pair_events: Mapping[tuple[str, str], int],
        *,
        now: datetime | None,
    ) -> list[TopologyRelation] | None:
        usable: list[_Candidate] = []
        for candidate in candidates:
            if candidate.n_observed < self.config.min_events:
                self._withheld.setdefault(device_id, Counter())[REASON_TOO_FEW_EVENTS] += 1
                continue
            if not candidate.dispersion_source():
                self._withheld.setdefault(device_id, Counter())[REASON_RESPONSE_WITHIN_NOISE] += 1
                continue
            stability = self._stability(candidate)
            if stability < self.config.stability_min:
                self._withheld.setdefault(device_id, Counter())[REASON_UNSTABLE] += 1
                continue
            usable.append(candidate)

        if not usable:
            return None

        overlap, budget_exceeded = self._overlap_report(usable)
        if budget_exceeded:
            self._withheld.setdefault(device_id, Counter())[REASON_BUDGET_EXCEEDED] += 1
            return None

        peer_ids = sorted(candidate.ct_id for candidate in usable)
        return [
            self._relation(
                candidate,
                conflicts=conflicts,
                overlap=overlap,
                peer_ids=peer_ids,
                n_events=pair_events.get((candidate.device_id, candidate.ct_id), 0),
                now=now,
            )
            for candidate in usable
        ]

    def _stability(self, candidate: _Candidate) -> float:
        ratios = candidate.dispersion_source()
        if not ratios:
            return 0.0
        centre = _median(ratios)
        if abs(centre) < 1e-9:
            return 0.0
        dispersion = _mad_w(ratios) / abs(centre)
        return max(0.0, min(1.0, 1.0 - dispersion))

    def _overlap_report(
        self, candidates: Sequence[_Candidate]
    ) -> tuple[dict[str, Any], bool]:
        """How the device's CTs relate to each other, and whether to refuse all.

        Four distinct outcomes, deliberately kept apart:

        * a base already claims the whole appliance (factor-adjusted or
          monitor-reported) -> overlap, not summable;
        * an unattributable pair (no solo event ever showed this device's share)
          -> not summable, because nobody knows what its share is;
        * two CTs report the same absolute level *and* the same contribution ->
          duplicate measurement of one conductor, not summable;
        * plain conductor contributions that add up to more than the device ->
          the evidence is physically impossible, so *nothing* is emitted.
        """
        ct_ids = sorted(candidate.ct_id for candidate in candidates)
        contributions = {
            candidate.ct_id: round(candidate.contribution, 6) for candidate in candidates
        }
        base_reason: str | None = None
        if any(candidate.basis in APPLIANCE_TOTAL_BASES for candidate in candidates):
            base_reason = OVERLAP_APPLIANCE_TOTAL
        elif any(not candidate.supporting for candidate in candidates):
            # A share was never isolated, so "these add up" is unknowable.
            base_reason = OVERLAP_UNATTRIBUTED
        elif self._has_duplicate(candidates):
            base_reason = OVERLAP_DUPLICATE
        total = sum(max(0.0, candidate.contribution) for candidate in candidates)
        if base_reason is None and total > 1.0 + self.config.magnitude_tolerance_frac:
            return (
                {
                    "ct_ids": ct_ids,
                    "contributions": contributions,
                    "summing_allowed": False,
                    "reason": REASON_BUDGET_EXCEEDED,
                },
                True,
            )
        reason = base_reason or OVERLAP_INDEPENDENT
        return (
            {
                "ct_ids": ct_ids,
                "contributions": contributions,
                "summing_allowed": base_reason is None,
                "reason": reason,
            },
            False,
        )

    def _has_duplicate(self, candidates: Sequence[_Candidate]) -> bool:
        """Two CTs measuring one conductor: same absolute level *and* same share.

        Same level alone is not enough (two legs of a balanced load sit at
        similar levels legitimately), and same share alone is not enough (two
        unrelated circuits can both carry the device's whole step).  Same level
        *and* same share, with matching per-event deltas, is a duplicated clamp.
        """
        tolerance = self.config.magnitude_tolerance_frac
        for index, first in enumerate(candidates):
            for second in candidates[index + 1 :]:
                if not first.levels or not second.levels:
                    continue
                level_a = _median(first.levels)
                level_b = _median(second.levels)
                denominator = max(abs(level_a), abs(level_b), 1.0)
                if abs(level_a - level_b) / denominator > tolerance:
                    continue
                if abs(first.contribution - second.contribution) > tolerance:
                    continue
                if len(first.raw_deltas) != len(second.raw_deltas):
                    continue
                if all(
                    abs(a - b) <= max(abs(a), abs(b), 1.0) * tolerance
                    for a, b in zip(first.raw_deltas, second.raw_deltas, strict=True)
                ):
                    return True
        return False

    def _relation(
        self,
        candidate: _Candidate,
        *,
        conflicts: Sequence[dict[str, Any]],
        overlap: dict[str, Any],
        peer_ids: Sequence[str],
        n_events: int,
        now: datetime | None,
    ) -> TopologyRelation:
        joint_devices: list[str] = []
        split_with: list[dict[str, Any]] = []
        for row in candidate.joint:
            devices = list(row.get("device_ids") or [candidate.device_id])
            for device in devices:
                if device != candidate.device_id and device not in joint_devices:
                    joint_devices.append(device)
            split_with.append(dict(row))

        if not candidate.supporting:
            # Only shared events: the CT proves it observes *one of* the devices
            # that moved together, never which one and never how much.
            verdict = VERDICT_AMBIGUOUS
        elif split_with:
            verdict = VERDICT_SPLIT
        elif len(candidate.supporting) == 1:
            verdict = VERDICT_SINGLE_EVENT
        else:
            verdict = VERDICT_SUPPORTED

        supporting = len(candidate.supporting)
        stability = self._stability(candidate)
        lag = _median(candidate.lags) if candidate.lags else 0.0
        confidence = self._confidence(
            n_events=n_events,
            stability=stability,
            basis_confidence=candidate.basis_confidence,
            lag_s=lag,
            n_conflicts=len(conflicts),
            verdict=verdict,
        )
        if verdict in (VERDICT_SINGLE_EVENT, VERDICT_AMBIGUOUS):
            confidence = min(confidence, SINGLE_EVENT_CAP)

        stats = {
            "n_events": n_events,
            "n_supporting": supporting,
            "n_conflicting": len(conflicts),
            "n_joint_events": len(split_with),
            "median_delta_w": round(_median(candidate.deltas), 6) if candidate.deltas else 0.0,
            "raw_median_delta_w": round(_median(candidate.raw_deltas), 6)
            if candidate.raw_deltas
            else 0.0,
            "median_device_delta_w": round(_median(candidate.device_deltas), 6)
            if candidate.device_deltas
            else 0.0,
            "mad_w": round(_mad_w(candidate.deltas), 6) if candidate.deltas else 0.0,
            # What the CT was *seen* to do during shared events, kept apart from
            # the attributed share: it is a real measurement, just not this
            # device's share, so it must never be read as a load estimate.
            "median_observed_delta_w": round(
                _median([float(row["scaled_delta_w"]) for row in candidate.joint]), 6
            )
            if candidate.joint
            else None,
            "median_observed_ratio": round(_median(candidate.joint_ratios), 6)
            if candidate.joint_ratios
            else None,
            "lag_s": round(lag, 3),
            "stability": round(stability, 6),
            "contribution": round(candidate.contribution, 6),
            "basis": candidate.basis,
            "applied_multiplier": candidate.applied_multiplier,
            "basis_confidence": candidate.basis_confidence,
            "ct_noise_w": round(_median(candidate.noises), 6) if candidate.noises else 0.0,
            "ct_level_w": round(_median(candidate.levels), 6) if candidate.levels else None,
            "noise_sigmas": self.config.noise_sigmas,
        }

        evidence: dict[str, Any] = {
            "verdict": verdict,
            "stats": stats,
            "supporting": list(candidate.supporting),
            "conflicting": list(conflicts),
            "overlap": overlap,
            "peer_ct_ids": list(peer_ids),
            "algorithm_version": self.config.algorithm_version,
            "config": self.config.as_metadata(),
            "isolation": {"clean_status": IsolationStatus.ISOLATED.value, "rule": "isolated_only"},
        }
        if split_with:
            evidence["split_with"] = split_with
        if verdict == VERDICT_AMBIGUOUS:
            evidence["ambiguous_with"] = sorted(joint_devices)

        valid_from: datetime | None = None
        for stamp in (
            *(_aware(datetime.fromisoformat(row["occurred_at"])) for row in candidate.joint),
            *(
                _aware(datetime.fromisoformat(row["occurred_at"]))
                for row in candidate.supporting
            ),
            *(
                _aware(datetime.fromisoformat(row["occurred_at"]))
                for row in conflicts
            ),
        ):
            if valid_from is None or stamp < valid_from:
                valid_from = stamp

        return TopologyRelation(
            relation_id=_relation_id(
                candidate.device_id, candidate.ct_id, self.config.algorithm_version
            ),
            kind=RelationKind.DEVICE_CT_ASSOCIATION,
            subject_id=candidate.device_id,
            object_id=candidate.ct_id,
            status=TopologyStatus.CANDIDATE,
            confidence=confidence,
            evidence=evidence,
            requires_confirmation=True,
            declared_by="inference",
            created_at=_assertion_instant(now, valid_from),
            valid_from=valid_from,
            valid_to=None,
            algorithm_version=self.config.algorithm_version,
        )

    def _confidence(
        self,
        *,
        n_events: int,
        stability: float,
        basis_confidence: float,
        lag_s: float,
        n_conflicts: int,
        verdict: str,
    ) -> float:
        """The documented product; see the module docstring for the reading."""
        repeatability = 1.0 - 0.5 ** max(n_events, 0)
        lag_term = 1.0
        if self.config.max_lag_s > 0.0:
            lag_term = 1.0 - 0.5 * min(1.0, max(0.0, lag_s) / self.config.max_lag_s)
        ambiguity = {
            VERDICT_AMBIGUOUS: AMBIGUOUS_MULTIPLIER,
            VERDICT_SPLIT: SPLIT_MULTIPLIER,
        }.get(verdict, 1.0)
        numerator = repeatability * stability * basis_confidence * lag_term * ambiguity
        denominator = 1.0 + self.config.conflict_penalty * max(0, n_conflicts)
        return max(0.0, min(MAX_CONFIDENCE, numerator / denominator))

    # ------------------------------------------------------------------ #
    # 3. withdraw / report
    # ------------------------------------------------------------------ #
    def withdraw(
        self, relation: TopologyRelation, reason: str, *, now: datetime | None = None
    ) -> TopologyRelation:
        """Invalidate a relation with ``valid_to``, keeping its evidence.

        Nothing is deleted: the assertion, its support and its conflicts remain
        readable, and the withdrawal itself is recorded with a reason and an
        instant.  ``valid_to`` is set to ``now`` (never to the relation's own
        ``valid_from``), so a historical reader can still ask what was claimed
        before it was withdrawn.
        """
        if not str(reason).strip():
            raise ValueError("a withdrawal must state a reason")
        moment = now or utcnow()
        payload = dict(relation.evidence)
        payload["withdrawal"] = {
            "reason": str(reason),
            "at": moment.isoformat(),
            "previous_status": _value(relation.status),
        }
        return TopologyRelation(
            relation_id=relation.relation_id,
            kind=relation.kind,
            subject_id=relation.subject_id,
            object_id=relation.object_id,
            status=TopologyStatus.REJECTED,
            confidence=relation.confidence,
            evidence=payload,
            requires_confirmation=relation.requires_confirmation,
            declared_by=relation.declared_by,
            created_at=relation.created_at,
            valid_from=relation.valid_from,
            valid_to=moment,
            algorithm_version=relation.algorithm_version,
        )

    def withheld(
        self,
        observations: Iterable[Observation],
        *,
        registry: FactorRegistry | None = None,
        now: datetime | None = None,
    ) -> dict[str, dict[str, int]]:
        """Why no relation was emitted for a device, as ``device -> reason -> n``.

        Counts are observations for observation-level reasons and decisions for
        pair-level ones, so ``budget_exceeded`` is 1 for a device whose CTs
        collectively disprove a relation while ``opposite_direction`` is the
        number of events that contradicted.

        The report covers the *whole* decision, not only the last step: an
        event excluded during partitioning (contaminated, no CT sample, basis
        unestablished) is counted even though it never reached pair assembly.
        When ``registry`` is omitted and the same observations were just
        analysed, the registry from that analysis is reused so the report
        explains the relations the caller actually saw.
        """
        rows = list(observations)
        remembered = self._last
        reuse = (
            remembered[1]
            if registry is None and remembered is not None and remembered[0] == rows
            else registry
        )
        self.analyze(rows, registry=reuse, now=now)
        return {
            device: dict(sorted(counts.items()))
            for device, counts in self._withheld.items()
            if counts
        }


def _relation_id(device_id: str, ct_id: str, algorithm_version: str) -> str:
    """Deterministic, name-free identity for one typed edge.

    Only the two endpoints and the algorithm version participate: a display
    name, a panel label or a re-ordered configuration cannot change it, so
    ``persist`` is idempotent and a re-run updates rather than duplicates.
    """
    digest = hashlib.sha256(
        f"device_ct_association|{device_id}|{ct_id}|{algorithm_version}".encode()
    ).hexdigest()
    return f"dca_{digest[:24]}"


def persist(relations: Iterable[TopologyRelation], database: Any) -> int:
    """Store relations through ``insert_topology_relation``; idempotent by id.

    Returns the number of relations written.  A withdrawn relation is stored
    with its ``valid_to`` and ``REJECTED`` status, so re-persisting the same
    relation id updates the interval instead of creating a second row or
    erasing the withdrawal.
    """
    count = 0
    for relation in relations:
        database.insert_topology_relation(relation)
        count += 1
    return count


__all__ = [
    "AMBIGUOUS_MULTIPLIER",
    "MAX_CONFIDENCE",
    "MAD_TO_SIGMA",
    "SINGLE_EVENT_CAP",
    "SPLIT_MULTIPLIER",
    "AssociationConfig",
    "AssociationFinder",
    "Observation",
    "persist",
]
