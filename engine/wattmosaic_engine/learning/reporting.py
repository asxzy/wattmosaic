"""Honest learning-pipeline accounting: accepted vs rejected samples.

Why this exists
---------------
``learning_active`` used to mean "a learning loop exists".  A live installation
reported it **ON** while the topology was still a ``candidate``: every sample was
therefore stamped ``topology_quality:candidate``, the filter rejected it as
``ambiguous_topology`` (correct behaviour), ``device_models`` stayed empty and
``model_estimated_power`` stayed 0 W forever.  The pipeline was running and
accepting nothing — indistinguishable from "learning" for a user reading a
dashboard.

This module adds the missing observable: two monotonic counters (seen/accepted),
a bounded per-reason tally, and an ``effective_state``:

* ``off``      -- the pipeline does not exist, or the user paused it;
* ``learning`` -- running and accepting samples;
* ``blocked``  -- running but rejecting every sample (or ~all of them), or
  running on an unconfirmed topology, which the filter rejects by construction.

Cost is O(1) per sample (one integer increment, one dict increment) and O(k) per
report with ``k <= MAX_REJECTION_LABELS`` (16).  Nothing is stored per sample, so
a week of rejections costs exactly the same memory as one.

The rejection *rules* are deliberately untouched: an unconfirmed topology must
reject samples, because the association itself is unverified.  Only the
reporting changes — and ``blocked`` names the remedy in plain words.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..contracts import SampleRejection

#: ``effective_state`` values.  Deliberately distinct from the HA binary state:
#: ``blocked`` is a *running* pipeline that can never train a model.
STATE_LEARNING = "learning"
STATE_BLOCKED = "blocked"
STATE_OFF = "off"
EFFECTIVE_STATES: frozenset[str] = frozenset({STATE_LEARNING, STATE_BLOCKED, STATE_OFF})

#: At most this many distinct rejection labels are remembered.  The contract
#: currently has 11 ``SampleRejection`` values, so the bound is theoretical — it
#: exists so a future/unknown reason cannot make the tally grow with traffic.
MAX_REJECTION_LABELS = 16
OTHER_LABEL = "other"
#: Samples that raised inside the filter.  Counted so ``seen == accepted +
#: rejected`` always holds (one bad sample must not vanish from the accounting).
FILTER_ERROR_LABEL = "filter_error"

#: A run that has rejected *this* share of at least ``ALMOST_ALL_MIN_SAMPLES``
#: samples is "blocked", not "learning with a few bad samples".
ALMOST_ALL_REJECTED = 0.95
ALMOST_ALL_MIN_SAMPLES = 10

#: Topology qualities the sample filter rejects as ``ambiguous_topology``.
UNCONFIRMED_TOPOLOGY_QUALITIES: frozenset[str] = frozenset(
    {"candidate", "ambiguous", "unconfirmed", "unknown"}
)

#: The user-facing remedy.  It names the exact entity the integration creates so
#: a dashboard can explain itself without a wiki link.
CONFIRM_REMEDY = (
    "Confirm the topology: in Home Assistant press "
    "button.wattmosaic_confirm_topology (WattMosaic Engine → Confirm topology), "
    "or re-run the WattMosaic setup wizard and confirm the CT mapping."
)


def _label(reason: Any) -> str:
    """A stable string label for a rejection reason."""
    if reason is None:
        return "rejected_no_reason"
    value = getattr(reason, "value", reason)
    text = str(value).strip().lower()
    return text or "rejected_no_reason"


class LearningAccounting:
    """Monotonic sample accounting with a bounded reason tally.

    Thread-safety: the engine's inference loop is single-threaded (asyncio), and
    every counter update is a single bytecode-level increment of a plain int, so
    no lock is needed.  Reads take a copy, so a report can never be torn.
    """

    __slots__ = ("_seen", "_accepted", "_rejected", "_reasons", "_max_labels")

    def __init__(self, max_labels: int = MAX_REJECTION_LABELS) -> None:
        self._seen = 0
        self._accepted = 0
        self._rejected = 0
        self._reasons: dict[str, int] = {}
        self._max_labels = max(1, int(max_labels))

    # -- recording ------------------------------------------------------- #
    def record(self, accepted: bool, reason: Any = None) -> None:
        """Account for one evaluated sample.  O(1)."""
        self._seen += 1
        if accepted:
            self._accepted += 1
            return
        self._rejected += 1
        label = _label(reason)
        # Reserve one slot for the overflow bucket so the tally is bounded by
        # ``max_labels`` *including* ``other``.  The contract has 11 rejection
        # reasons and the default bound is 16, so no real reason is ever
        # folded — the bound only guards against an unknown/future reason.
        if label not in self._reasons and len(self._reasons) >= self._max_labels - 1:
            label = OTHER_LABEL
        self._reasons[label] = self._reasons.get(label, 0) + 1

    def record_batch(self, accepted: int, rejected: Iterable[Any] = ()) -> None:
        """Account for a batch: ``accepted`` samples plus the rejected reasons."""
        for _ in range(max(0, int(accepted))):
            self.record(True)
        for reason in rejected:
            self.record(False, reason)

    def reset(self) -> None:
        """Drop every counter (used by tests and by an explicit model reset)."""
        self._seen = 0
        self._accepted = 0
        self._rejected = 0
        self._reasons.clear()

    # -- reading --------------------------------------------------------- #
    @property
    def seen(self) -> int:
        return self._seen

    @property
    def accepted(self) -> int:
        return self._accepted

    @property
    def rejected(self) -> int:
        return self._rejected

    def dominant(self) -> tuple[str | None, int]:
        """The most frequent rejection reason.  Ties break alphabetically."""
        if not self._reasons:
            return None, 0
        label, count = min(self._reasons.items(), key=lambda item: (-item[1], item[0]))
        return label, count

    def reasons(self) -> dict[str, int]:
        """A copy of the tally, most frequent first (bounded by construction)."""
        return dict(sorted(self._reasons.items(), key=lambda item: (-item[1], item[0])))

    def snapshot(self) -> dict[str, Any]:
        """A cheap, JSON-safe view.  Copies at most ``max_labels`` entries."""
        dominant, count = self.dominant()
        return {
            "samples_seen": self._seen,
            "samples_accepted": self._accepted,
            "samples_rejected": self._rejected,
            "acceptance_rate": (self._accepted / self._seen) if self._seen else None,
            "rejection_reasons": self.reasons(),
            "dominant_rejection_reason": dominant,
            "dominant_rejection_count": count,
        }


def build_learning_status(
    *,
    enabled: bool,
    active: bool,
    samples_seen: int = 0,
    samples_accepted: int = 0,
    samples_rejected: int = 0,
    rejection_reasons: dict[str, int] | None = None,
    topology_quality: str | None = None,
) -> dict[str, Any]:
    """Derive the honest learning status from the counters and the topology.

    ``topology_quality`` is what the sample filter will stamp on each sample
    (``integration._topology_quality``).  A ``candidate`` topology means the
    filter rejects *every* sample by construction, so the pipeline is reported
    ``blocked`` even before the first rejection arrives — the user then learns
    why on the very first poll instead of after a week of silence.
    """
    seen = max(0, int(samples_seen))
    accepted = min(max(0, int(samples_accepted)), seen)
    rejected = min(max(0, int(samples_rejected)), seen - accepted)
    reasons = {
        str(key): int(value)
        for key, value in (rejection_reasons or {}).items()
        if isinstance(key, str) and int(value) > 0
    }
    dominant: str | None = None
    dominant_count = 0
    if reasons:
        dominant, dominant_count = min(
            reasons.items(), key=lambda item: (-item[1], item[0])
        )

    quality = str(topology_quality).strip().lower() if topology_quality else None
    rate = (accepted / seen) if seen else None

    state = STATE_LEARNING
    blocked_by: str | None = None
    remedy: str | None = None

    if not enabled:
        state = STATE_OFF
        summary = "Learning is disabled in the engine configuration; no samples are collected."
    elif not active:
        state = STATE_OFF
        summary = "Learning is paused; no samples are collected or trained."
    elif (
        (seen and accepted == 0)
        or (
            seen >= ALMOST_ALL_MIN_SAMPLES
            and rate is not None
            and rate <= (1.0 - ALMOST_ALL_REJECTED)
        )
    ):
        state = STATE_BLOCKED
        blocked_by = "observed_rejections"
    elif accepted == 0 and quality in UNCONFIRMED_TOPOLOGY_QUALITIES:
        state = STATE_BLOCKED
        blocked_by = "unconfirmed_topology"

    if state == STATE_BLOCKED:
        if dominant is None and blocked_by == "unconfirmed_topology":
            dominant = SampleRejection.AMBIGUOUS_TOPOLOGY.value
            dominant_count = 0
        if dominant == SampleRejection.AMBIGUOUS_TOPOLOGY.value:
            remedy = CONFIRM_REMEDY
        if blocked_by == "unconfirmed_topology":
            summary = (
                "Learning is running but the topology is still a candidate, so every "
                "sample is rejected as ambiguous_topology and no model can be trained."
            )
        else:
            share = "100%" if accepted == 0 else f"{(1 - (rate or 0.0)) * 100:.0f}%"
            top = f", mostly {dominant}" if dominant else ""
            summary = (
                f"Learning is running but {share} of samples are rejected "
                f"({rejected} of {seen}){top}; no model can be trained from them."
            )
    elif state == STATE_LEARNING:
        if seen == 0:
            summary = (
                "Learning is running; no sample has been offered yet."
            )
        else:
            summary = (
                f"Learning is running and accepting samples "
                f"({accepted} of {seen} accepted, {(rate or 0.0) * 100:.0f}%)."
            )

    return {
        "enabled": bool(enabled),
        "active": bool(active),
        "effective_state": state,
        "samples_seen": seen,
        "samples_accepted": accepted,
        "samples_rejected": rejected,
        "acceptance_rate": rate,
        "rejection_reasons": reasons,
        "dominant_rejection_reason": dominant,
        "dominant_rejection_count": dominant_count,
        "blocked_by": blocked_by,
        "topology_quality": quality,
        "summary": summary,
        "remedy": remedy,
    }


__all__ = [
    "ALMOST_ALL_MIN_SAMPLES",
    "ALMOST_ALL_REJECTED",
    "CONFIRM_REMEDY",
    "EFFECTIVE_STATES",
    "FILTER_ERROR_LABEL",
    "LearningAccounting",
    "MAX_REJECTION_LABELS",
    "OTHER_LABEL",
    "STATE_BLOCKED",
    "STATE_LEARNING",
    "STATE_OFF",
    "UNCONFIRMED_TOPOLOGY_QUALITIES",
    "build_learning_status",
]
