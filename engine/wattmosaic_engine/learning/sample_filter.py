"""Learning observation quality gate v2 (L4, design §9 / HB §13.1).

The seven checks run in a deterministic order and every rejection reason is
reachable; an observation from a relationship in ``SUSPECTED_CHANGE`` is held
PROVISIONAL, and a non-identifiable member yields group-only evidence.  Pure:
all external inputs (including the relationship-confirmation callable and the
clock) are injected, and no legacy sample object is involved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from ..contracts import (
    EventGroupStatus,
    IdentifiabilityStatus,
    LearningObservation,
)
from .thresholds import LearningThresholds

# --------------------------------------------------------------------------- #
# Learning gate v2 (L4, design §9 / HB §13.1).
#
# The v2 gate below operates on device-attributed ``LearningObservation``
# objects and applies the seven checks in design §9's order.  It is pure: every
# external input -- including T4's ``edges_confirmed_for`` callable and the
# observation's timestamp -- is injected, and it never imports runtime.fakes.
# --------------------------------------------------------------------------- #


class GateRejection(str, Enum):
    """Rejection reasons of the v2 gate, in design §9 check order."""

    MEASUREMENT_QUALITY = "measurement_quality"
    GROUP_NOT_CONFIRMED = "group_not_confirmed"
    NON_IDENTIFIABLE = "non_identifiable"
    UNKNOWN_ALTERNATIVE_SHARE = "unknown_alternative_share"
    RELATIONSHIP_SUSPECTED_CHANGE = "relationship_suspected_change"
    NOT_INDEPENDENT = "not_independent"
    MODEL_INCONSISTENT = "model_inconsistent"

    @classmethod
    def order(cls) -> tuple[GateRejection, ...]:
        """The ordered rejection ledger (design §9)."""
        return (
            cls.MEASUREMENT_QUALITY,
            cls.GROUP_NOT_CONFIRMED,
            cls.NON_IDENTIFIABLE,
            cls.UNKNOWN_ALTERNATIVE_SHARE,
            cls.RELATIONSHIP_SUSPECTED_CHANGE,
            cls.NOT_INDEPENDENT,
            cls.MODEL_INCONSISTENT,
        )


#: Backward-compatible ordered ledger; mirrors ``GateRejection.order()``.
_GATE_CHECK_ORDER: tuple[GateRejection, ...] = GateRejection.order()


@dataclass(slots=True)
class GateInputs:
    """External evidence the gate needs, injected by the caller.

    ``edges_confirmed_for`` is T4's per-subject relationship-confirmation
    callable.  It is injected as a plain callable so production depends only on
    that seam, not on T4's not-yet-merged module.  ``edges_confirmed`` is a
    scalar fallback for callers that have already resolved it for the subject;
    when both are present the callable wins.
    """

    group_status: EventGroupStatus | str
    measurement_quality_ok: bool = True
    unknown_share: float | None = None
    edges_confirmed: bool | None = None
    relationship_suspected: bool = False
    independent_event: bool = True
    model_consistent: bool = True
    model_abs_error_w: float | None = None
    model_sigma_w: float | None = None
    edges_confirmed_for: Callable[[str], bool] | None = None


@dataclass(slots=True)
class GateDecision:
    """Outcome of one gate evaluation."""

    accepted: bool
    status: str = "confirmed"
    rejection_reason: GateRejection | None = None
    detail: str = ""
    group_only: bool = False

    def apply(self, observation: LearningObservation) -> LearningObservation:
        """Return a copy of ``observation`` stamped with this decision."""
        updated = observation.model_copy(deep=True)
        updated.status = self.status
        updated.rejection_reason = (
            self.rejection_reason.value if self.rejection_reason is not None else None
        )
        note = f"gate:{self.status}" if self.accepted else f"gate_rejected:{self.rejection_reason.value}"
        updated.attributes = {**updated.attributes, "gate_note": note}
        return updated


_NON_INDIVIDUAL_IDENTIFIABILITY: frozenset[str] = frozenset(
    {
        IdentifiabilityStatus.NON_IDENTIFIABLE.value,
        IdentifiabilityStatus.INSUFFICIENT_EVIDENCE.value,
        IdentifiabilityStatus.GROUP_IDENTIFIABLE.value,
        IdentifiabilityStatus.PARTIALLY_IDENTIFIABLE.value,
    }
)

_CONFIRMED_GROUP_STATUSES: frozenset[str] = frozenset(
    {EventGroupStatus.CONFIRMED.value, EventGroupStatus.REVISED.value}
)


def evaluate_gate(
    observation: LearningObservation,
    inputs: GateInputs,
    *,
    clock: Callable[[], datetime],
    thresholds: LearningThresholds | None = None,
) -> GateDecision:
    """Apply the v2 learning gate in design §9 order.

    The gate never raises on a malformed observation: one bad observation must
    not stop a batch (spec §21).  A missing numeric measurement is rejected as
    ``measurement_quality`` rather than silently treated as zero (HB §18).
    ``clock`` is injected for future time-based checks and to keep the entry
    point free of wall-clock reads; the observation's own ``created_at`` is the
    immutable evidence timestamp and is never re-derived.
    """
    t = thresholds or LearningThresholds()
    _ = clock  # injected for symmetry with the rest of v3; no wall-clock reads.

    group_status = _name(inputs.group_status)
    checks: dict[GateRejection, tuple[bool, str, str]] = {}

    # 1. Reading quality -- a missing delta is not a zero delta.
    measurements_ok = bool(inputs.measurement_quality_ok) and observation.delta_w is not None
    if observation.delta_w is None:
        mq_detail = "delta_missing"
    elif not inputs.measurement_quality_ok:
        mq_detail = "measurement_quality_failed"
    else:
        mq_detail = "ok"
    checks[GateRejection.MEASUREMENT_QUALITY] = (
        not measurements_ok,
        mq_detail,
        "rejected",
    )

    # 2. Group CONFIRMED (not PROVISIONAL).
    group_ok = group_status in _CONFIRMED_GROUP_STATUSES
    checks[GateRejection.GROUP_NOT_CONFIRMED] = (
        not group_ok,
        f"group_status={group_status}",
        "provisional" if not group_ok else "confirmed",
    )

    # 3. Subject IDENTIFIABLE -- otherwise the observation feeds the group only.
    ident = _name(observation.identifiability)
    non_individual = ident in _NON_INDIVIDUAL_IDENTIFIABILITY
    checks[GateRejection.NON_IDENTIFIABLE] = (
        non_individual,
        f"identifiability={ident}",
        "confirmed",
    )

    # 4. Unknown-alternative share must be below the configured ceiling.
    share = inputs.unknown_share
    share_bad = share is not None and share >= t.max_unknown_share
    checks[GateRejection.UNKNOWN_ALTERNATIVE_SHARE] = (
        share_bad,
        "unknown_share>=limit" if share_bad else "unknown_share_ok",
        "rejected",
    )

    # 5. Relationship edges not SUSPECTED_CHANGE (T4's per-subject callable).
    # A declared suspicion and a confirmed-false relationship are equivalent:
    # neither may update a confirmed model, both are held PROVISIONAL.
    relationship_bad = bool(inputs.relationship_suspected) or (
        inputs.edges_confirmed is not None and not inputs.edges_confirmed
    )
    if inputs.edges_confirmed_for is not None:
        # The per-subject callable is authoritative for relationship validity;
        # an explicit SUSPECTED_CHANGE declaration still holds provisional.
        relationship_bad = relationship_bad or not inputs.edges_confirmed_for(
            observation.subject_id
        )
    checks[GateRejection.RELATIONSHIP_SUSPECTED_CHANGE] = (
        relationship_bad,
        "relationship_suspected_change" if relationship_bad else "relationship_confirmed",
        "provisional" if relationship_bad else "confirmed",
    )

    # 6. Independent event (no unexplained sibling sub-event on the same readers).
    checks[GateRejection.NOT_INDEPENDENT] = (
        not inputs.independent_event,
        "concurrent_unexplained_sub_event" if not inputs.independent_event else "independent",
        "rejected",
    )

    # 7. Consistency with the active model within ``sigma`` sigmas.
    consistent = bool(inputs.model_consistent)
    error = inputs.model_abs_error_w
    sigma = inputs.model_sigma_w
    if error is not None and sigma is not None:
        consistent = error <= t.model_consistency_sigma * abs(sigma)
    checks[GateRejection.MODEL_INCONSISTENT] = (
        not consistent,
        "outside_model_interval" if not consistent else "within_model_interval",
        "rejected",
    )

    decided: GateRejection | None = None
    detail = ""
    status = "rejected"
    for reason in _GATE_CHECK_ORDER:
        failed, reason_detail, failed_status = checks[reason]
        if failed:
            decided = reason
            detail = reason_detail
            # A group still PROVISIONAL or a relationship in SUSPECTED_CHANGE
            # is *held* PROVISIONAL (retained for later reconciliation), not
            # rejected outright.  Hard failures keep status ``rejected``.
            status = failed_status
            break

    if decided is GateRejection.NON_IDENTIFIABLE:
        return GateDecision(
            accepted=False,
            status="rejected",
            rejection_reason=decided,
            detail=detail,
            group_only=True,
        )

    accepted = decided is None
    return GateDecision(
        accepted=accepted,
        status="confirmed" if accepted else status,
        rejection_reason=decided,
        detail=detail,
    )


def _name(value: Any) -> str:
    return str(getattr(value, "value", value)) if value is not None else ""

__all__ = ["GateDecision", "GateInputs", "GateRejection", "evaluate_gate"]
