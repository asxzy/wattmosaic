"""Evidence-frozen promotion decisions (L5, DEC-32/34, DEF-03).

A candidate is promoted only on the *same frozen evidence set* used to evaluate
the active model, after at least ``MIN_INDEPENDENT_EVENTS`` independent confirmed
events (counted per event group, never per sample).  Per-class acceptance
criteria replace the old single R² gate so a well-behaved fixed load with a
flat holdout can promote (DEF-03).  A frozen model blocks promotion; freeze
never blocks inference.  Pure: no database and no wall clock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts import (
    EvidenceRef,
    LearningObservation,
    ModelClass,
    PowerModelEvent,
    PowerModelVersion,
)

# --------------------------------------------------------------------------- #
# L5: promotion v2 on a frozen evidence set (DEC-32, DEC-34, DEF-03).
# --------------------------------------------------------------------------- #

#: Minimum number of *independent confirmed events* (counted by event group)
#: before a candidate may change an ACTIVE model (design §9, DEC-32).
MIN_INDEPENDENT_EVENTS = 5


def _name(value: Any) -> str:
    return str(getattr(value, "value", value)) if value is not None else ""


@dataclass(slots=True)
class PromotionDecisionV2:
    """Outcome of a pure, evidence-frozen promotion decision."""

    promoted: bool
    reason: str
    independent_events: int = 0
    review_suggested: bool = False
    class_change_proposal: ModelClass | None = None
    events: tuple[PowerModelEvent, ...] = ()
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "promoted": self.promoted,
            "reason": self.reason,
            "independent_events": self.independent_events,
            "review_suggested": self.review_suggested,
            "class_change_proposal": _name(self.class_change_proposal) or None,
            "events": [e.model_dump() for e in self.events],
            "notes": list(self.notes),
        }


def independent_event_count(observations: Sequence[LearningObservation]) -> int:
    """Count independent confirmed events by their event-group id.

    Several observations from the same event group are one independent event;
    counting samples instead would let a single event promote a model (the
    defect this task fixes).
    """
    groups = {
        _name(getattr(getattr(o, "event_group_ref", None), "id", None))
        for o in observations
        if _name(getattr(o, "status", "")) == "confirmed"
    }
    groups.discard("")
    return len(groups)


def class_change(
    candidate: PowerModelVersion,
    active: PowerModelVersion | None,
) -> bool:
    """Whether the candidate proposes a different model class than active."""
    if active is None:
        return False
    return _name(candidate.model_class) != _name(active.model_class)


def evaluate_promotion_v2(
    candidate: PowerModelVersion,
    active: PowerModelVersion | None,
    observations: Sequence[LearningObservation],
    *,
    frozen: bool,
    clock: Any,
    min_events: int = MIN_INDEPENDENT_EVENTS,
) -> PromotionDecisionV2:
    """Decide promotion on the *same frozen evidence set* for both versions.

    Pure: no DB and no wall clock (``clock`` is injected).  It records the
    evidence-id set on ``candidate.evidence_refs``, enforces the ≥5 independent
    confirmed events rule, applies the per-class criterion from design §9, and
    compares candidate vs active on the frozen set.  A first version or a class
    change yields a "review suggested" *diagnostic event* (DEC-32), never a
    block; a class change additionally records a class-change *proposal*
    (DEC-34).  A frozen model blocks promotion entirely.
    """
    from .validation import (
        ClassCriteria,
        compare_frozen,
        evaluate_frozen,
        per_class_accepts,
    )

    def events(*items: tuple[str, str]) -> tuple[PowerModelEvent, ...]:
        now = clock()
        return tuple(
            PowerModelEvent(version_id=candidate.version_id, event=kind, reason=reason, at=now)
            for kind, reason in items
        )

    # Record the frozen evidence-id set on the version (design §9 step 2).
    ids = sorted(
        {
            str(o.observation_id)
            for o in observations
            if _name(getattr(o, "status", "")) == "confirmed"
        }
    )
    candidate.evidence_refs = [
        EvidenceRef(kind="observation", id=observation_id) for observation_id in ids
    ]

    independent = independent_event_count(observations)

    if frozen:
        return PromotionDecisionV2(
            promoted=False,
            reason="frozen",
            independent_events=independent,
            events=events(("candidate", "blocked_by_frozen_model")),
            notes=["frozen blocks promotion, not inference"],
        )

    if independent < min_events:
        return PromotionDecisionV2(
            promoted=False,
            reason=f"insufficient_independent_events:{independent}<{min_events}",
            independent_events=independent,
            events=events(("candidate", f"insufficient_independent_events:{independent}")),
            notes=["a single event never changes an ACTIVE version"],
        )

    criteria = ClassCriteria()
    candidate_eval = evaluate_frozen(candidate.spec, observations, candidate.model_class)
    if candidate_eval.metrics.get("n", 0.0) < criteria.min_frozen_samples:
        return PromotionDecisionV2(
            promoted=False,
            reason="insufficient_frozen_evidence",
            independent_events=independent,
            events=events(("candidate", "insufficient_frozen_evidence")),
        )

    active_eval = None
    active_metrics = None
    if active is not None:
        active_eval = evaluate_frozen(active.spec, observations, active.model_class)
        active_metrics = active_eval.metrics

    accepted, criterion_reason = per_class_accepts(
        candidate.model_class, candidate_eval.metrics, active_metrics, criteria
    )
    if not accepted:
        return PromotionDecisionV2(
            promoted=False,
            reason=f"per_class_criterion_failed:{criterion_reason}",
            independent_events=independent,
            events=events(("rejected", criterion_reason)),
        )

    comparison = compare_frozen(candidate_eval.metrics, active_metrics)
    if not comparison["better"]:
        return PromotionDecisionV2(
            promoted=False,
            reason=comparison["reason"],
            independent_events=independent,
            events=events(("rejected", comparison["reason"])),
        )

    proposed_class = candidate.model_class if class_change(candidate, active) else None
    review = active is None or proposed_class is not None

    diagnostic: list[tuple[str, str]] = []
    if active is not None:
        diagnostic.append(("superseded", f"superseded_by:{candidate.version_id}"))
    if proposed_class is not None:
        # DEC-34: a class change is a diagnostic suggestion only, never applied
        # silently to the subject's declared LoadShape.
        diagnostic.append(
            ("candidate", f"class_change_proposed:{_name(proposed_class)}")
        )
    if review:
        first = "first_version" if active is None else "class_change"
        diagnostic.append(("candidate", f"review_suggested:{first}"))
    diagnostic.append(("activated", comparison["reason"]))

    return PromotionDecisionV2(
        promoted=True,
        reason="promoted",
        independent_events=independent,
        review_suggested=review,
        class_change_proposal=proposed_class,
        events=events(*diagnostic),
        notes=[criterion_reason, comparison["reason"], *candidate_eval.notes],
    )


__all__ = [
    "MIN_INDEPENDENT_EVENTS",
    "PromotionDecisionV2",
    "class_change",
    "evaluate_promotion_v2",
    "independent_event_count",
]
