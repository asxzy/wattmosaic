"""Candidate generation for an event group (I1, HB §7.1, §7.5).

Builds the candidate set from all nine HB §7.1 sources and always adds one
``unknown_scope`` candidate per affected reader scope.  Candidates are merged
per (subject kind, subject id), topology-filtered conservatively, ranked by
source count then prior fit, capped at 32 (unknown candidates are exempt) and
returned with truncation recorded on the result.

Production dependencies are only the K04 contracts and K08 runtime types.
``runtime.fakes`` is never imported.  In particular:

* plug steps (source 5) are recognised from :class:`SubEventKind.PLUG_STEP`;
* unknown load profiles (source 6) are recognised from the subject kind on a
  sub-event reference, a ``profiles_for_scope`` capability on the mapping/residual
  view, or the injected ``unknown_profiles`` mapping (a fake until T6 exists);
* operator-visible subjects (source 7) come from the operator's subject list;
* delayed responses (source 8) come from ``pending_delayed`` (a list of
  ``(subject, earlier_group_opened_at)`` pairs, or a callable) plus the
  ``DELAYED_RESPONSE`` tag on a sub-event reference;
* known device groups (source 9) come from ``groups`` (an
  ``EstablishedStructure.groups()`` mapping or ``{group_id: member_ids}``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from wattmosaic_engine.contracts import (
    CandidateHypothesis,
    CandidateSource,
    EventGroup,
    SubEvent,
    SubEventKind,
)
from wattmosaic_engine.runtime.interfaces import (
    MappingView,
    ModelSetView,
    ObservationOperator,
)

from .topology_filter import PROVISIONAL_FLAG, evaluate_topology

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 32
DEFAULT_DELAY_WINDOW_S = 300.0


# --------------------------------------------------------------------------- #
# Optional injected capabilities (all satisfied by K08 fakes where applicable).
# --------------------------------------------------------------------------- #


@runtime_checkable
class ProfileView(Protocol):
    def profiles_for_scope(self, reader_id: str) -> Sequence[Any]: ...


@runtime_checkable
class EstablishedGroupsView(Protocol):
    def groups(self) -> Mapping[str, Sequence[str]]: ...


PendingDelayed = (
    Mapping[tuple[str, str], datetime]
    | Sequence[tuple[tuple[str, str], datetime]]
    | Callable[[datetime], Sequence[tuple[tuple[str, str], datetime]]]
)
UnknownProfiles = Mapping[str, Sequence[Any]]
DeviceGroups = Mapping[str, Sequence[str]] | EstablishedGroupsView


@dataclass
class CandidateSet:
    """The full result of :func:`generate_candidates`.

    ``candidates`` contains only the surviving (unfiltered) candidates;
    ``rejected`` records every rejection with its reason; ``truncated`` counts
    how many non-unknown candidates were dropped by the cap.  Unknown-scope
    candidates are always present and are never truncated.
    """

    candidates: list[CandidateHypothesis] = field(default_factory=list)
    rejected: list[CandidateHypothesis] = field(default_factory=list)
    truncated: int = 0
    unknown_candidates: list[CandidateHypothesis] = field(default_factory=list)

    @property
    def all_hypotheses(self) -> list[CandidateHypothesis]:
        return [*self.candidates, *self.rejected]


# --------------------------------------------------------------------------- #
# Internal accumulator — merge sources per subject.
# --------------------------------------------------------------------------- #


@dataclass
class _Acc:
    subject_kind: str
    subject_id: str
    sources: list[CandidateSource] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)

    def add(self, source: CandidateSource, transition: str | None = None) -> None:
        if source not in self.sources:
            self.sources.append(source)
        if transition is not None and transition not in self.transitions:
            self.transitions.append(transition)

    @property
    def key(self) -> tuple[str, str]:
        return (self.subject_kind, self.subject_id)


def _kind(sub_event: Any) -> str:
    """The string value of a sub-event kind (Model uses ``use_enum_values``)."""
    value = getattr(sub_event, "kind", None)
    return str(getattr(value, "value", value))


def _subject(ref: Any) -> tuple[str, str] | None:
    """Extract a ``(kind, id)`` pair from a sub-event reference or record."""
    if ref is None:
        return None
    if isinstance(ref, str):
        if ":" in ref:
            kind, identifier = ref.split(":", 1)
            if kind in {"device", "component", "group", "unknown_profile", "unknown_scope"}:
                return (kind, identifier)
        return ("device", ref)
    for attr in ("subject", "target", "subject_pair"):
        pair = getattr(ref, attr, None)
        if pair is not None and isinstance(pair, (tuple, list)) and len(pair) == 2:
            return (str(pair[0]), str(pair[1]))
    for attr in ("subject_kind", "kind"):
        subject_kind = getattr(ref, attr, None)
        if isinstance(subject_kind, str) and subject_kind in {
            "device",
            "component",
            "group",
            "unknown_profile",
            "unknown_scope",
        }:
            subject_id = getattr(ref, "subject_id", None) or getattr(ref, "target_id", None)
            if subject_id is not None:
                return subject_kind, str(subject_id)
    for attr in ("entity_id", "subject_id", "target_id", "id"):
        raw_id = getattr(ref, attr, None)
        if raw_id is not None:
            text_id = str(raw_id)
            if ":" in text_id:
                id_kind, _, rest = text_id.partition(":")
                if id_kind in {"device", "component", "group", "unknown_profile", "unknown_scope"}:
                    return (id_kind, rest)
            return ("device", text_id)
    return None


def _ref_source(ref: Any) -> Any:
    return getattr(ref, "source", None) if ref is not None else None


def _candidate_id(key: tuple[str, str], group: EventGroup) -> str:
    return f"cand::{group.group_id}:{key[0]}::{key[1]}"


def _acced(accs: dict[tuple[str, str], _Acc], key: tuple[str, str] | None) -> _Acc | None:
    if key is None:
        return None
    acc = accs.get(key)
    if acc is None:
        acc = _Acc(key[0], key[1])
        accs[key] = acc
    return acc


def _affected_readers(group: EventGroup) -> tuple[str, ...]:
    return tuple(sorted({delta.reader_id for delta in group.reader_deltas}))


def _deepest_scope(reader: str, operator: ObservationOperator) -> str:
    """Deepest reader scope for an affected reader.

    An affected reader is itself the deepest reader that saw the step (its
    ancestors are broader aggregates).  We return the reader as-is; the operator
    is accepted so that a future, richer scope view can refine this without
    changing callers.
    """
    return reader


def _source_ha(accs: dict[tuple[str, str], _Acc], group: EventGroup) -> None:
    for sub in group.sub_events:
        if _kind(sub) == SubEventKind.HA_STATE.value:
            source, transition = CandidateSource.HA_STATE, _transition(sub.ref)
        elif _kind(sub) == SubEventKind.HA_ATTRIBUTE.value:
            source, transition = CandidateSource.HA_ATTRIBUTE, None
        elif _kind(sub) == SubEventKind.PLUG_STEP.value:
            source, transition = CandidateSource.PLUG, None
        else:
            continue
        acc = _acced(accs, _subject(sub.ref))
        if acc is not None:
            acc.add(source, transition)

    for sub in group.sub_events:
        source = _ref_source(sub.ref)
        tag = getattr(source, "kind", None) or getattr(source, "source", None)
        if isinstance(tag, str) and "plug" in tag.lower():
            acc = _acced(accs, _subject(sub.ref))
            if acc is not None:
                acc.add(CandidateSource.PLUG)


def _transition(ref: Any) -> str | None:
    for attr in ("to_state", "state", "transition"):
        value = getattr(ref, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _source_profiles(
    accs: dict[tuple[str, str], _Acc],
    group: EventGroup,
    models: ModelSetView,
    unknown_profiles: UnknownProfiles,
    mapping: MappingView,
    affected: tuple[str, ...],
    operator: ObservationOperator,
) -> None:
    # Source 3 — subjects with a learned profile whose DeltaPrior fits ±3σ of
    # some affected reader's delta.  A declared prior always contributes a
    # candidate; a fit is computed when both a prior and a usable delta exist.
    for reader in affected:
        delta = next((d.delta_w for d in group.reader_deltas if d.reader_id == reader), None)
        sigma = next((d.sigma_w for d in group.reader_deltas if d.reader_id == reader), None)
        if delta is None:
            continue
        for sub in group.sub_events:
            key = _subject(sub.ref)
            if key is None or key[0] not in {"device", "component"}:
                continue
            prior = models.transition_prior(
                key[1], _transition(sub.ref) or "on", getattr(sub.ref, "attributes_delta", {}) or {}
            )
            if prior is None:
                continue
            if sigma is not None and abs(delta - prior.mean_w) > 3.0 * max(sigma, prior.sigma_w):
                continue
            acc = _acced(accs, key)
            if acc is not None:
                acc.add(CandidateSource.LEARNED_PROFILE, _transition(sub.ref))

    # Source 4 — prior mapping edges from any affected reader.
    for reader in affected:
        for key in mapping.subjects_on(reader):
            acc = _acced(accs, (str(key[0]), str(key[1])))
            if acc is not None:
                acc.add(CandidateSource.PRIOR_MAPPING)

    # Source 6 — Unknown Load Profiles at affected scopes (T6 view; a fake
    # mapping injected here until T6 lands).
    provider = unknown_profiles if unknown_profiles else None
    view = provider if isinstance(provider, ProfileView) else None
    for reader in affected:
        profiles: Sequence[Any]
        if view is not None:
            profiles = view.profiles_for_scope(reader)
        elif provider is not None:
            profiles = provider.get(reader, ())
        else:
            profiles = ()
        for profile in profiles:
            key = _subject(profile)
            if key is None:
                key = ("unknown_profile", str(getattr(profile, "profile_id", reader)))
            if key[0] != "unknown_profile":
                key = ("unknown_profile", key[1])
            acc = _acced(accs, key)
            if acc is not None:
                acc.add(CandidateSource.UNKNOWN_PROFILE)

    # Source 7 — operator-visible subjects.
    for key in operator.subjects:
        acc = _acced(accs, (str(key[0]), str(key[1])))
        if acc is not None:
            acc.add(CandidateSource.TOPOLOGY_VISIBLE)


def _source_pending(
    accs: dict[tuple[str, str], _Acc],
    group: EventGroup,
    pending_delayed: PendingDelayed | None,
) -> None:
    if pending_delayed is None:
        return
    now = group.opened_at
    window = timedelta(seconds=DEFAULT_DELAY_WINDOW_S)

    entries: Sequence[tuple[tuple[str, str], datetime]] = ()
    if callable(pending_delayed):
        entries = pending_delayed(now)
    elif isinstance(pending_delayed, Mapping):
        entries = [
            (key, opened)
            for key, opened in pending_delayed.items()
            if isinstance(key, tuple) and len(key) == 2
        ]
    else:
        entries = list(pending_delayed)

    for key, opened in entries:
        if opened is not None and now - opened <= window:
            acc = _acced(accs, (str(key[0]), str(key[1])))
            if acc is not None:
                acc.add(CandidateSource.DELAYED_RESPONSE)

    for sub in group.sub_events:
        source = _ref_source(sub.ref)
        tag = getattr(source, "kind", None) if source is not None else None
        if isinstance(tag, str) and "delay" in tag.lower():
            acc = _acced(accs, _subject(sub.ref))
            if acc is not None:
                acc.add(CandidateSource.DELAYED_RESPONSE)


def _source_groups(
    accs: dict[tuple[str, str], _Acc],
    groups: DeviceGroups | None,
) -> None:
    if groups is None:
        return
    mapping: Mapping[str, Sequence[str]] = (
        groups.groups() if isinstance(groups, EstablishedGroupsView) else groups
    )
    for group_id, members in mapping.items():
        acc = _acced(accs, ("group", str(group_id)))
        if acc is not None:
            acc.add(CandidateSource.DEVICE_GROUP)
        for member in members:
            member_key = _subject(member) or ("device", str(member))
            acc = _acced(accs, member_key)
            if acc is not None:
                acc.add(CandidateSource.DEVICE_GROUP)


def _prior_fit(
    acc: _Acc,
    group: EventGroup,
    models: ModelSetView,
) -> float:
    """A scalar prior-fit score (higher is better); no 0 W for absent priors."""
    if acc.subject_kind not in {"device", "component"}:
        return 0.0
    transition = acc.transitions[0] if acc.transitions else "on"
    prior = models.transition_prior(acc.subject_id, transition, {})
    if prior is None:
        return 0.0
    deltas = [d.delta_w for d in group.reader_deltas if d.delta_w is not None]
    if not deltas:
        return 0.0
    best = min(abs(delta - prior.mean_w) / max(prior.sigma_w, 1e-6) for delta in deltas)
    return -best  # closer fit => higher score


def _build_hypothesis(
    acc: _Acc,
    group: EventGroup,
    models: ModelSetView,
) -> CandidateHypothesis:
    terms: dict[str, float] = {"source_count": float(len(acc.sources))}
    prior_fit = _prior_fit(acc, group, models)
    terms["prior_fit"] = prior_fit
    return CandidateHypothesis(
        candidate_id=_candidate_id(acc.key, group),
        subject_kind=acc.subject_kind,  # type: ignore[arg-type]
        subject_id=acc.subject_id,
        sources=list(acc.sources),
        transition=acc.transitions[0] if acc.transitions else None,
        evidence_terms=terms,
        filtered=False,
        filter_reason=None,
    )


def _add_unknown_scopes(
    result: CandidateSet,
    group: EventGroup,
    operator: ObservationOperator,
    affected: tuple[str, ...],
) -> None:
    for reader in affected:
        scope = _deepest_scope(reader, operator)
        key = ("unknown_scope", scope)
        acc = _Acc(key[0], key[1])
        acc.add(CandidateSource.UNKNOWN_SCOPE)
        hypothesis = CandidateHypothesis(
            candidate_id=_candidate_id(key, group),
            subject_kind="unknown_scope",
            subject_id=scope,
            sources=[CandidateSource.UNKNOWN_SCOPE],
            transition=None,
            evidence_terms={"source_count": 1.0, "prior_fit": 0.0},
        )
        result.unknown_candidates.append(hypothesis)


def generate_candidates(
    group: EventGroup,
    operator: ObservationOperator,
    models: ModelSetView,
    mapping: MappingView,
    unknown_profiles: UnknownProfiles | None = None,
    groups: DeviceGroups | None = None,
    pending_delayed: PendingDelayed | None = None,
    ring: Collection[SubEvent] | None = None,
) -> CandidateSet:
    """Generate, filter, rank and cap candidate hypotheses for ``group``.

    All inputs are pure snapshots; the clock is injected via ``group.opened_at``/``ring``.
    ``unknown_profiles``, ``groups``, ``pending_delayed`` and ``ring`` are optional
    because their producers (T6, I4, P1/P5) may not exist yet — when absent, the
    corresponding source simply contributes nothing except the guaranteed
    unknown-scope candidates.
    """
    affected = _affected_readers(group)
    accs: dict[tuple[str, str], _Acc] = {}

    _source_ha(accs, group)
    if ring:
        for sub in ring:
            if _kind(sub) == SubEventKind.HA_STATE.value:
                acc = _acced(accs, _subject(sub.ref))
                if acc is not None:
                    acc.add(CandidateSource.HA_STATE, _transition(sub.ref))
    _source_profiles(accs, group, models, unknown_profiles or {}, mapping, affected, operator)
    _source_pending(accs, group, pending_delayed)
    _source_groups(accs, groups)

    result = CandidateSet()
    _add_unknown_scopes(result, group, operator, affected)

    survivors: list[CandidateHypothesis] = []
    for key in sorted(accs):
        acc = accs[key]
        if key[0] == "unknown_scope":
            continue
        hypothesis = _build_hypothesis(acc, group, models)
        verdict = evaluate_topology(key, affected, operator, mapping)
        if verdict.filtered:
            hypothesis.filtered = True
            hypothesis.filter_reason = verdict.reason
            result.rejected.append(hypothesis)
            continue
        if verdict.soft_flag:
            hypothesis.evidence_terms = {
                **hypothesis.evidence_terms,
                PROVISIONAL_FLAG: 1.0,
            }
        survivors.append(hypothesis)

    survivors.sort(
        key=lambda h: (
            -len(h.sources),
            -h.evidence_terms.get("prior_fit", float("-inf")),
            h.candidate_id,
        )
    )
    keep = survivors[:MAX_CANDIDATES]
    result.truncated = len(survivors) - len(keep)
    if result.truncated:
        logger.info(
            "candidate cap reached for group %s: kept %d of %d (truncated %d)",
            group.group_id,
            len(keep),
            len(survivors),
            result.truncated,
        )
    result.candidates = [*keep, *result.unknown_candidates]
    return result


__all__ = [
    "MAX_CANDIDATES",
    "PROVISIONAL_FLAG",
    "CandidateSet",
    "DeviceGroups",
    "PendingDelayed",
    "ProfileView",
    "UnknownProfiles",
    "generate_candidates",
]
