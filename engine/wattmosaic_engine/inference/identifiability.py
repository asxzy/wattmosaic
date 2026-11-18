"""Identifiability from H plus independent event history (I4, HB §8.4, DEC-19).

Identifiability is a property of the *measurement system and the event
history*, never of whether a transition happened in this cycle::

    X = [ H(T, M, t)          ]   rows of the current event group
        [ H_row * 1_event     ]   one row block per historical independent event

The historical block for an event is the operator row of each reader the event
observed, weighted by which candidates actually transitioned in that event.
Two devices that always switch together therefore add only copies of the same
row: the rank stays one, the null space is ``(1, -1, ...)`` and only the group
sum ``1_G^T x`` is identified.  Events in which one of them switched alone add
genuinely independent directions; enough of those separate the group.

States (design §7):

``IDENTIFIABLE``
    null-space share at or below :data:`NULL_SHARE_TOL`, and — when a posterior
    covariance exists — a standard deviation below ``max(5 W, 10 % |x|)``.
``PARTIALLY_IDENTIFIABLE``
    an unidentifiable member of a coupling group whose sum *is* identified and
    that does not cover the whole candidate set, so the sum is not published as
    a first-class device group.
``GROUP_IDENTIFIABLE``
    an inseparable group that *is* the event's whole candidate set and whose sum
    is identified: the group estimate is published, the members are not.
``NON_IDENTIFIABLE``
    a member of an inseparable group whose sum is not identified, or of an
    identified group covering the whole candidate set.
``INSUFFICIENT_EVIDENCE``
    no usable rows, or fewer than :data:`MIN_HISTORY_EVENTS` independent events
    touching the coupling group.

``group_id`` is ``sha256("\\0".join(sorted(member_ids)))[:16]``: stable while the
membership holds, across cycles and restarts.

This module re-uses :func:`electrical.solver.analyse_identifiability` for the
SVD/rank/null-space work and :func:`disaggregation.group_fit._co_toggle_groups`
for the history-derived co-toggle components; neither is modified.  It is pure:
the clock is ``operator.at`` and history is injected.

Null policy (HB §18): a member that is not identified has ``point_w = None``;
there is no ``0.0`` placeholder anywhere in this module.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from wattmosaic_engine.contracts import (
    Bounds,
    ConfidenceVectorV3,
    DeviceEstimate,
    DeviceGroupEstimate,
    EstimateStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceMethod,
    VersionTriple,
)
from wattmosaic_engine.disaggregation.group_fit import _co_toggle_groups
from wattmosaic_engine.electrical.solver import Identifiability, analyse_identifiability
from wattmosaic_engine.runtime.interfaces import (
    HistoryRow,
    IdentifiabilityHistory,
    JointSolution,
    ObservationOperator,
)

from .bounds import (
    DEFAULT_N_SIGMA,
    SignClass,
    compute_bounds,
    group_sum_constraint,
)

__all__: list[str] = [
    "CORRELATION_TOL",
    "HISTORY_WINDOW",
    "MAX_HISTORY_EVENTS",
    "MIN_HISTORY_EVENTS",
    "NULL_SHARE_TOL",
    "EstimateAssembly",
    "GroupIdentifiability",
    "IdentifiabilityAnalyzer",
    "IdentifiabilityReport",
    "assemble_estimates",
    "member_point_w",
    "subject_group_id",
]

#: How far back independent events count as identifiability evidence.
HISTORY_WINDOW = timedelta(days=90)
#: Hard cap on history rows consulted per analysis (design §7).
MAX_HISTORY_EVENTS = 500
#: Fewer independent events than this cannot state anything about a couple.
MIN_HISTORY_EVENTS = 2
#: A column whose null-space share is at or below this is individually known.
NULL_SHARE_TOL = 0.05
#: Two candidate columns above this |correlation| co-vary too much to separate.
CORRELATION_TOL = 0.95
#: Posterior bound used for the IDENTIFIABLE decision (design §7).
POSTERIOR_SIGMA_ABS_W = 5.0
POSTERIOR_SIGMA_REL = 0.10

#: Numerical tolerance for "this functional is orthogonal to the null space".
_FUNCTIONAL_TOL = 1e-6
#: Numerical tolerance for a non-zero matrix/coefficient.
_NONZERO_TOL = 1e-9


def subject_group_id(member_ids: Sequence[str]) -> str:
    """Stable id of a device group: ``sha256(sorted(member_ids))[:16]``."""
    ids = sorted(str(member_id) for member_id in member_ids)
    if not ids:
        raise ValueError("a device group needs at least one member")
    return hashlib.sha256("\0".join(ids).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class GroupIdentifiability:
    """One inseparable group whose identified sum may be published."""

    group_id: str
    member_ids: tuple[str, ...]
    identifiability: IdentifiabilityStatus
    sum_w: float | None
    sigma_w: float | None
    source_reader_ids: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    covers_all_candidates: bool


@dataclass(frozen=True, slots=True)
class IdentifiabilityReport:
    """Classification of every candidate and of every coupling group."""

    subject_ids: tuple[str, ...]
    subjects: Mapping[str, IdentifiabilityStatus]
    groups: tuple[GroupIdentifiability, ...]
    rank: int
    n_experiments: int
    n_history_events: int
    condition_number: float | None
    singular_values: tuple[float, ...]
    null_space_share: Mapping[str, float]
    column_correlation: Mapping[tuple[str, str], float]
    usable_rows: tuple[int, ...]
    experiment_matrix: np.ndarray

    def status_of(self, subject_id: str) -> IdentifiabilityStatus:
        return self.subjects[subject_id]

    def group_of(self, subject_id: str) -> GroupIdentifiability | None:
        for group in self.groups:
            if subject_id in group.member_ids:
                return group
        return None

    def identifiable_ids(self) -> tuple[str, ...]:
        return tuple(
            subject_id
            for subject_id in self.subject_ids
            if self.subjects[subject_id] is IdentifiabilityStatus.IDENTIFIABLE
        )


class IdentifiabilityAnalyzer:
    """Build the experiment matrix and classify every candidate (HB §8.4).

    Every parameter has a design-§7 default and is injectable, so a test can
    exercise a stricter or looser rule without patching internals.  The class is
    stateless: repeated calls with the same inputs return equal reports.
    """

    def __init__(
        self,
        *,
        n_sigma: float = DEFAULT_N_SIGMA,
        history_window: timedelta = HISTORY_WINDOW,
        max_history_events: int = MAX_HISTORY_EVENTS,
        min_history_events: int = MIN_HISTORY_EVENTS,
        null_share_tol: float = NULL_SHARE_TOL,
        correlation_tol: float = CORRELATION_TOL,
        posterior_sigma_abs_w: float = POSTERIOR_SIGMA_ABS_W,
        posterior_sigma_rel: float = POSTERIOR_SIGMA_REL,
    ) -> None:
        if max_history_events < 0:
            raise ValueError("max_history_events must be non-negative")
        if min_history_events < 0:
            raise ValueError("min_history_events must be non-negative")
        if not 0.0 <= null_share_tol <= 1.0:
            raise ValueError("null_share_tol must be in [0, 1]")
        if not 0.0 <= correlation_tol <= 1.0:
            raise ValueError("correlation_tol must be in [0, 1]")
        self.n_sigma = float(n_sigma)
        self.history_window = history_window
        self.max_history_events = int(max_history_events)
        self.min_history_events = int(min_history_events)
        self.null_share_tol = float(null_share_tol)
        self.correlation_tol = float(correlation_tol)
        self.posterior_sigma_abs_w = float(posterior_sigma_abs_w)
        self.posterior_sigma_rel = float(posterior_sigma_rel)

    # ------------------------------------------------------------------ #
    def analyze(
        self,
        operator: ObservationOperator,
        solution: JointSolution,
        candidates: Sequence[str],
        history: IdentifiabilityHistory,
    ) -> IdentifiabilityReport:
        """Classify ``candidates`` from ``operator`` plus independent history.

        ``candidates`` are subject ids (the ``subject_id`` half of the
        operator's ``subjects``); order is irrelevant and duplicates collapse.
        Only candidate columns enter the experiment matrix.
        """
        subject_ids = tuple(dict.fromkeys(str(candidate) for candidate in candidates))
        if not subject_ids:
            raise ValueError("identifiability analysis needs at least one candidate")
        columns = _candidate_columns(operator, subject_ids)
        usable_rows = _usable_rows(operator, solution)
        current = _current_block(operator, usable_rows, columns)
        events, history_rows = _history_block(
            operator,
            subject_ids,
            columns,
            usable_rows,
            history,
            since=operator.at - self.history_window,
            limit=self.max_history_events,
        )
        blocks = [block for block in (current, history_rows) if block.size]
        matrix = np.vstack(blocks) if blocks else np.zeros((0, len(subject_ids)))

        ident = analyse_identifiability(
            matrix,
            subject_ids,
            null_share_tol=self.null_share_tol,
            n_device_columns=len(subject_ids),
        )
        null_share = {
            subject_id: float(share)
            for subject_id, share in zip(subject_ids, ident.null_share, strict=True)
        }
        correlation = _column_correlation(matrix, subject_ids)

        if ident.n_rows == 0 or not np.any(np.abs(matrix) > _NONZERO_TOL):
            statuses = {
                subject_id: IdentifiabilityStatus.INSUFFICIENT_EVIDENCE
                for subject_id in subject_ids
            }
            return self._report(
                subject_ids,
                statuses,
                ident,
                matrix=matrix,
                null_share=null_share,
                correlation=correlation,
                groups=(),
                usable_rows=usable_rows,
                n_history_events=len(events),
            )

        sigma = _posterior_sigma(solution, columns, subject_ids)
        points = _solved_points(solution, columns, subject_ids)
        unidentifiable = [
            subject_id
            for subject_id in subject_ids
            if null_share[subject_id] > self.null_share_tol
        ]
        components = _coupling_components(
            ident,
            subject_ids,
            unidentifiable,
            correlation,
            events,
            correlation_tol=self.correlation_tol,
        )
        groups = self._groups(
            components,
            subject_ids,
            unidentifiable,
            ident,
            points,
            sigma,
            operator,
            usable_rows,
            columns,
            events,
        )

        statuses: dict[str, IdentifiabilityStatus] = {}
        for subject_id in subject_ids:
            if subject_id not in set(unidentifiable):
                statuses[subject_id] = (
                    IdentifiabilityStatus.IDENTIFIABLE
                    if self._individually_identified(
                        points.get(subject_id), sigma.get(subject_id)
                    )
                    else IdentifiabilityStatus.PARTIALLY_IDENTIFIABLE
                )
                continue
            component = _component_of(components, subject_id)
            if len(_events_touching(events, component)) < self.min_history_events:
                statuses[subject_id] = IdentifiabilityStatus.INSUFFICIENT_EVIDENCE
            elif len(component) == len(subject_ids):
                # The whole candidate set is one inseparable group: the group
                # sum (when identified) is first-class, the members are not.
                statuses[subject_id] = IdentifiabilityStatus.NON_IDENTIFIABLE
            else:
                # Some linear combination including this subject is known, but
                # the subject itself is not: a partial identifiability.
                statuses[subject_id] = IdentifiabilityStatus.PARTIALLY_IDENTIFIABLE

        published = tuple(
            group
            for group in groups
            if group.sum_w is not None
            and all(
                statuses.get(member) is IdentifiabilityStatus.NON_IDENTIFIABLE
                for member in group.member_ids
            )
        )
        return self._report(
            subject_ids,
            statuses,
            ident,
            matrix=matrix,
            null_share=null_share,
            correlation=correlation,
            groups=published,
            usable_rows=usable_rows,
            n_history_events=len(events),
        )

    # ------------------------------------------------------------------ #
    def _individually_identified(
        self, point_w: float | None, sigma_w: float | None
    ) -> bool:
        """Posterior check that accompanies a zero null-space share."""
        if sigma_w is None or not np.isfinite(sigma_w):
            # No posterior is *not* evidence of a large error: the share already
            # decided that nothing can absorb this subject's variation.
            return True
        limit = max(
            self.posterior_sigma_abs_w,
            self.posterior_sigma_rel * abs(point_w) if point_w is not None else 0.0,
        )
        return sigma_w < limit

    def _groups(
        self,
        components: Sequence[tuple[str, ...]],
        subject_ids: Sequence[str],
        unidentifiable: Sequence[str],
        ident: Identifiability,
        points: Mapping[str, float | None],
        sigma: Mapping[str, float | None],
        operator: ObservationOperator,
        usable_rows: Sequence[int],
        columns: Mapping[str, int],
        events: Sequence[HistoryRow],
    ) -> tuple[GroupIdentifiability, ...]:
        groups: list[GroupIdentifiability] = []
        unknown = set(unidentifiable)
        for component in components:
            members = tuple(sorted(member for member in component if member in unknown))
            if len(members) < 2:
                continue
            readers = _source_readers(operator, usable_rows, columns, members)
            refs = _evidence_refs(events, members)
            covers_all = len(members) == len(subject_ids)
            if not _functional_identified(ident, members, subject_ids):
                groups.append(
                    GroupIdentifiability(
                        group_id=subject_group_id(members),
                        member_ids=members,
                        identifiability=IdentifiabilityStatus.NON_IDENTIFIABLE,
                        sum_w=None,
                        sigma_w=None,
                        source_reader_ids=readers,
                        evidence_refs=refs,
                        covers_all_candidates=covers_all,
                    )
                )
                continue
            # A first-class DeviceGroupEstimate is published only when the
            # inseparable group *is* the event's whole candidate set.  When some
            # candidates are identified individually, the group sum is a partial
            # identifiability fact about the rest (design §7: PARTIALLY_
            # IDENTIFIABLE), not a group the members belong to.
            groups.append(
                GroupIdentifiability(
                    group_id=subject_group_id(members),
                    member_ids=members,
                    identifiability=(
                        IdentifiabilityStatus.GROUP_IDENTIFIABLE
                        if covers_all
                        else IdentifiabilityStatus.PARTIALLY_IDENTIFIABLE
                    ),
                    sum_w=group_sum_w(points, members),
                    sigma_w=_sum_sigma(sigma, members),
                    source_reader_ids=readers,
                    evidence_refs=refs,
                    covers_all_candidates=covers_all,
                )
            )
        return tuple(groups)

    def _report(
        self,
        subject_ids: Sequence[str],
        statuses: Mapping[str, IdentifiabilityStatus],
        ident: Identifiability,
        *,
        matrix: np.ndarray,
        null_share: Mapping[str, float],
        correlation: Mapping[tuple[str, str], float],
        groups: Sequence[GroupIdentifiability],
        usable_rows: Sequence[int],
        n_history_events: int,
    ) -> IdentifiabilityReport:
        condition = ident.condition_number
        return IdentifiabilityReport(
            subject_ids=tuple(subject_ids),
            subjects=dict(statuses),
            groups=tuple(groups),
            rank=int(ident.rank),
            n_experiments=int(matrix.shape[0]),
            n_history_events=int(n_history_events),
            condition_number=float(condition) if np.isfinite(condition) else None,
            singular_values=tuple(float(value) for value in ident.singular_values),
            null_space_share=dict(null_share),
            column_correlation=dict(correlation),
            usable_rows=tuple(usable_rows),
            experiment_matrix=matrix,
        )


# --------------------------------------------------------------------------- #
# Experiment matrix.
# --------------------------------------------------------------------------- #


def _candidate_columns(
    operator: ObservationOperator, subject_ids: Sequence[str]
) -> dict[str, int]:
    """Column of each candidate in ``operator.subjects``."""
    wanted = set(subject_ids)
    columns: dict[str, int] = {}
    for index, (_kind, subject_id) in enumerate(operator.subjects):
        if subject_id in wanted and subject_id not in columns:
            columns[subject_id] = index
    missing = [subject_id for subject_id in subject_ids if subject_id not in columns]
    if missing:
        raise ValueError(f"candidates are not subjects of the operator: {missing}")
    return columns


def _usable_rows(operator: ObservationOperator, solution: JointSolution) -> tuple[int, ...]:
    """Rows the current solve actually used.

    ``solution.active_rows`` wins when non-empty; otherwise every row the
    operator did not exclude (a stale reader carries an ``excluded_reason``).
    """
    n_rows = operator.H.shape[0]
    active = tuple(int(row) for row in solution.active_rows if 0 <= int(row) < n_rows)
    if active:
        return tuple(dict.fromkeys(active))
    return tuple(
        index for index, row in enumerate(operator.rows) if row.excluded_reason is None
    )


def _current_block(
    operator: ObservationOperator,
    rows: Sequence[int],
    columns: Mapping[str, int],
) -> np.ndarray:
    if not rows:
        return np.zeros((0, len(columns)))
    cols = list(columns.values())
    block = np.asarray(operator.H, dtype=float)[list(rows)][:, cols]
    return block[np.any(np.abs(block) > _NONZERO_TOL, axis=1)]


def _history_block(
    operator: ObservationOperator,
    subject_ids: Sequence[str],
    columns: Mapping[str, int],
    usable_rows: Sequence[int],
    history: IdentifiabilityHistory,
    *,
    since: datetime,
    limit: int,
) -> tuple[list[HistoryRow], np.ndarray]:
    """Rows for past independent events, each weighted by its transition mask."""
    if limit == 0:
        return [], np.zeros((0, len(columns)))
    events = [event for event in history.events_touching(list(subject_ids), since) if event.at >= since]
    events.sort(key=lambda event: (event.at, event.group_id))
    if len(events) > limit:
        events = events[-limit:]
    row_index = {row.reader_id: index for index, row in enumerate(operator.rows)}
    usable = set(usable_rows)
    cols = list(columns.values())
    block_rows: list[np.ndarray] = []
    for event in events:
        transitions = set(event.subject_ids)
        mask = np.array(
            [1.0 if subject_id in transitions else 0.0 for subject_id in subject_ids]
        )
        if not np.any(mask):
            continue
        readers = list(event.reader_ids) or [
            operator.rows[index].reader_id for index in sorted(usable)
        ]
        for reader_id in readers:
            index = row_index.get(reader_id)
            if index is None or index not in usable:
                continue
            row = np.asarray(operator.H[index], dtype=float)[cols] * mask
            if np.any(np.abs(row) > _NONZERO_TOL):
                block_rows.append(row)
    if not block_rows:
        return events, np.zeros((0, len(columns)))
    return events, np.vstack(block_rows)


def _column_correlation(
    matrix: np.ndarray, subject_ids: Sequence[str]
) -> dict[tuple[str, str], float]:
    """Absolute pairwise column correlation.

    A column with no variation carries no correlation, so the pair is absent
    rather than reported as ``0.0`` (HB §18).
    """
    n_columns = matrix.shape[1]
    if matrix.shape[0] == 0 or n_columns < 2:
        return {}
    norms = np.linalg.norm(matrix, axis=0)
    result: dict[tuple[str, str], float] = {}
    for first in range(n_columns):
        for second in range(first + 1, n_columns):
            if norms[first] == 0.0 or norms[second] == 0.0:
                continue
            cosine = float(
                np.dot(matrix[:, first], matrix[:, second])
                / (norms[first] * norms[second])
            )
            result[(subject_ids[first], subject_ids[second])] = abs(cosine)
    return result


def _posterior_sigma(
    solution: JointSolution,
    columns: Mapping[str, int],
    subject_ids: Sequence[str],
) -> dict[str, float | None]:
    """Per-subject posterior sigma, ``None`` when the covariance is absent.

    ``JointSolution.cov`` is ``None`` for a singular system (K08); the honest
    value is then ``None`` and the decision falls back to the null-space share
    alone.  Nothing is defaulted to ``0.0``.
    """
    cov = solution.cov
    if cov is None:
        return {subject_id: None for subject_id in subject_ids}
    cov = np.asarray(cov, dtype=float)
    if cov.ndim != 2 or cov.shape[0] <= max(columns.values()):
        return {subject_id: None for subject_id in subject_ids}
    sigma: dict[str, float | None] = {}
    for subject_id in subject_ids:
        value = float(cov[columns[subject_id], columns[subject_id]])
        sigma[subject_id] = float(np.sqrt(value)) if value > 0.0 else None
    return sigma


def _sum_sigma(sigma: Mapping[str, float | None], members: Collection[str]) -> float | None:
    """Posterior sigma of ``sum(x[members])`` under a diagonal approximation.

    A full covariance block would need the joint posterior; the diagonal is the
    honest available quantity and only widens a sum constraint, never publishes
    a value.
    """
    values = [sigma.get(member) for member in members]
    if any(value is None for value in values):
        return None
    return float(np.sqrt(sum(float(value) ** 2 for value in values if value is not None)))


def _solved_points(
    solution: JointSolution,
    columns: Mapping[str, int],
    subject_ids: Sequence[str],
) -> dict[str, float | None]:
    x = np.asarray(solution.x, dtype=float).reshape(-1)
    return {
        subject_id: float(x[column]) if column < x.size else None
        for subject_id, column in ((s, columns[s]) for s in subject_ids)
    }


def _coupling_components(
    ident: Identifiability,
    subject_ids: Sequence[str],
    unidentifiable: Sequence[str],
    correlation: Mapping[tuple[str, str], float],
    events: Sequence[HistoryRow],
    *,
    correlation_tol: float,
) -> tuple[tuple[str, ...], ...]:
    """Union-find over null-space support, high correlation and co-toggle history.

    Only *unidentifiable* subjects take part: a subject with a zero null-space
    share cannot be inside an inseparable group, and letting it join one would
    hide that it is known.
    """
    unknown = set(unidentifiable)
    candidates = [subject for subject in subject_ids if subject in unknown]
    if not candidates:
        return ()
    parent = {subject: subject for subject in candidates}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(first: str, second: str) -> None:
        root, other = find(first), find(second)
        if root != other:
            parent[other] = root

    basis = ident.null_space_basis
    for direction in range(basis.shape[1]):
        support = [
            str(subject_ids[row])
            for row in np.flatnonzero(np.abs(basis[: len(subject_ids), direction]) > 1e-6)
            if str(subject_ids[row]) in parent
        ]
        for first, second in zip(support, support[1:], strict=False):
            union(first, second)
    for (first, second), value in correlation.items():
        if value > correlation_tol and first in parent and second in parent:
            union(first, second)

    instants = _transition_instants(events, candidates)
    if instants:
        for component in _co_toggle_groups(instants):
            members = [member for member in component if member in parent]
            for first, second in zip(members, members[1:], strict=False):
                union(first, second)

    grouped: dict[str, list[str]] = {}
    for subject in candidates:
        grouped.setdefault(find(subject), []).append(subject)
    return tuple(tuple(sorted(members)) for members in grouped.values())


def _transition_instants(
    events: Sequence[HistoryRow], subjects: Sequence[str]
) -> dict[str, np.ndarray]:
    """Epoch seconds of every historical event touching each subject."""
    wanted = set(subjects)
    stamps: dict[str, list[float]] = {subject: [] for subject in subjects}
    for event in events:
        epoch = event.at.timestamp()
        for subject_id in event.subject_ids:
            if subject_id in wanted:
                stamps[subject_id].append(epoch)
    return {
        subject: np.asarray(sorted(values), dtype=float)
        for subject, values in stamps.items()
        if values
    }


def _component_of(components: Sequence[tuple[str, ...]], subject_id: str) -> tuple[str, ...]:
    for component in components:
        if subject_id in component:
            return component
    return (subject_id,)


def group_sum_w(
    points: Mapping[str, float | None], members: Sequence[str]
) -> float | None:
    """The solved sum of ``members`` when every member has a solved value.

    ``None`` as soon as one member has no value: a partial sum is not the
    group's sum, and publishing it under a group id would be a fabrication.
    """
    values = [points.get(member) for member in members]
    if any(value is None for value in values):
        return None
    return float(sum(value for value in values if value is not None))


def _events_touching(
    events: Sequence[HistoryRow], members: Collection[str]
) -> list[HistoryRow]:
    wanted = set(members)
    return [event for event in events if wanted.intersection(event.subject_ids)]


def _functional_identified(
    ident: Identifiability,
    members: Sequence[str],
    subject_ids: Sequence[str],
) -> bool:
    """Is ``1_G^T x`` orthogonal to every null-space direction?"""
    index = {subject: position for position, subject in enumerate(subject_ids)}
    rows = [index[member] for member in members if member in index]
    basis = ident.null_space_basis
    if basis.shape[1] == 0:
        return True
    projection = basis[rows, :].sum(axis=0)
    return bool(np.all(np.abs(projection) <= _FUNCTIONAL_TOL))


def _source_readers(
    operator: ObservationOperator,
    usable_rows: Sequence[int],
    columns: Mapping[str, int],
    members: Sequence[str],
) -> tuple[str, ...]:
    """Readers whose row has a non-zero coefficient on any member's column."""
    matrix = np.asarray(operator.H, dtype=float)
    readers: list[str] = []
    for row in usable_rows:
        for member in members:
            if abs(float(matrix[row, columns[member]])) > _NONZERO_TOL:
                reader_id = operator.rows[row].reader_id
                if reader_id not in readers:
                    readers.append(reader_id)
                break
    return tuple(readers)


def _evidence_refs(
    events: Sequence[HistoryRow], members: Collection[str]
) -> tuple[EvidenceRef, ...]:
    """Typed refs for the independent events that involved any member."""
    seen: set[tuple[str, str]] = set()
    unique: list[EvidenceRef] = []
    for event in _events_touching(events, members):
        key = ("event_group", event.group_id)
        if key not in seen:
            seen.add(key)
            unique.append(EvidenceRef(kind="event_group", id=event.group_id))
    return tuple(unique)


# --------------------------------------------------------------------------- #
# Estimate assembly (the group part of HB §8.4; I5 completes the status table).
# --------------------------------------------------------------------------- #


def member_point_w(
    *,
    solution_w: float | None,
    model_w: float | None,
    identifiability: IdentifiabilityStatus | str,
) -> float | None:
    """The *only* place a member may receive a number.

    A model prior may license a point only for a subject the analysis marked
    IDENTIFIABLE (DEC-19); for anything else both inputs are ignored and the
    answer is ``None``.  Keeping this in one named function is what makes the
    "equal split" and "fill from models" mutations observable.
    """
    if IdentifiabilityStatus(identifiability) is not IdentifiabilityStatus.IDENTIFIABLE:
        return None
    if solution_w is not None:
        return float(solution_w)
    if model_w is not None:
        return float(model_w)
    return None


@dataclass(frozen=True, slots=True)
class EstimateAssembly:
    """Group and member estimates published from one identifiability report."""

    device_estimates: tuple[DeviceEstimate, ...]
    group_estimates: tuple[DeviceGroupEstimate, ...]


def assemble_estimates(
    report: IdentifiabilityReport,
    operator: ObservationOperator,
    *,
    versions: VersionTriple,
    at: datetime,
    points: Mapping[str, float | None] | None = None,
    models: Mapping[str, float | None] | None = None,
    ceilings: Mapping[str, float | None] | None = None,
    signs: Mapping[str, SignClass] | None = None,
    n_sigma: float = DEFAULT_N_SIGMA,
) -> EstimateAssembly:
    """Turn a report into ``DeviceGroupEstimate``/``DeviceEstimate`` objects.

    ``points`` are values from the joint solve and ``models`` are model priors
    in watts.  Neither can give a non-identifiable member a number:
    :func:`member_point_w` ignores both for such a subject, and a member's
    interval comes from :func:`bounds.compute_bounds` alone.
    """
    columns = _candidate_columns(operator, report.subject_ids)
    point_map = dict(points or {})
    model_map = dict(models or {})
    sums = [
        group_sum_constraint(group.member_ids, group.sum_w, group.sigma_w, n_sigma=n_sigma)
        for group in report.groups
        if group.sum_w is not None
    ]
    identified = {
        subject_id: float(point_map[subject_id])
        for subject_id in report.identifiable_ids()
        if point_map.get(subject_id) is not None
    }
    bounds = compute_bounds(
        report.subject_ids,
        identified=identified,
        identifiability={s: report.subjects[s] for s in identified},
        sums=sums,
        signs=signs,
        ceilings=ceilings,
        n_sigma=n_sigma,
    )

    device_estimates: list[DeviceEstimate] = []
    for subject_id in report.subject_ids:
        status = report.subjects[subject_id]
        point_w = member_point_w(
            solution_w=point_map.get(subject_id),
            model_w=model_map.get(subject_id),
            identifiability=status,
        )
        group = report.group_of(subject_id)
        member_group_id = (
            group.group_id
            if group is not None
            and group.identifiability is IdentifiabilityStatus.GROUP_IDENTIFIABLE
            else None
        )
        member_bounds = bounds[subject_id]
        estimate = DeviceEstimate(
            subject_kind=_subject_kind(operator, columns[subject_id]),
            subject_id=subject_id,
            point_w=point_w,
            # A point interval cannot pass Pydantic's nested validation (the
            # `allow_point` gate is only open in the private Python constructor),
            # so the point is attached below through that constructor.
            bounds=(
                Bounds(basis=member_bounds.basis)
                if _is_point(member_bounds)
                else member_bounds
            ),
            status=(
                EstimateStatus.ESTIMATED if point_w is not None else EstimateStatus.UNKNOWN
            ),
            identifiability=status,
            group_id=member_group_id,
            method=(
                InferenceMethod.JOINT_DELTA if point_w is not None else InferenceMethod.NONE
            ),
            confidence=ConfidenceVectorV3(
                identifiability=_identifiability_score(report, subject_id)
            ),
            source_reader_ids=list(
                _source_readers(operator, report.usable_rows, columns, (subject_id,))
            ),
            evidence_refs=([] if group is None else list(group.evidence_refs)),
            versions=versions,
            updated_at=at,
        )
        if _is_point(member_bounds):
            estimate.bounds = member_bounds
        device_estimates.append(estimate)

    group_estimates: list[DeviceGroupEstimate] = []
    for group in report.groups:
        # Only a GROUP_IDENTIFIABLE group is published as a first-class
        # estimate; a partial group's sum is used to bound its members but is
        # not published (it would double-count an identified sibling).
        if group.sum_w is None or (
            group.identifiability is not IdentifiabilityStatus.GROUP_IDENTIFIABLE
        ):
            continue
        refs: list[EvidenceRef] = list(group.evidence_refs)
        for reader in group.source_reader_ids:
            ref = EvidenceRef(kind="observation", id=reader)
            if ref not in refs:
                refs.append(ref)
        group_estimates.append(
            DeviceGroupEstimate(
                group_id=group.group_id,
                member_ids=list(group.member_ids),
                point_w=group.sum_w,
                bounds=(
                    # No posterior width is available: an absent bound is
                    # honest (HB §18), and the contract forbids a zero-width
                    # interval from being nested in an estimate.  The point
                    # value is the identified sum; its interval is unknown.
                    Bounds(basis="group_sum")
                    if group.sigma_w is None
                    else Bounds(
                        lower_w=group.sum_w - n_sigma * group.sigma_w,
                        upper_w=group.sum_w + n_sigma * group.sigma_w,
                        basis="group_sum",
                    )
                ),
                identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
                source_reader_ids=list(group.source_reader_ids),
                evidence_refs=refs,
                versions=versions,
                updated_at=at,
            )
        )
    return EstimateAssembly(
        device_estimates=tuple(device_estimates),
        group_estimates=tuple(group_estimates),
    )


def _subject_kind(operator: ObservationOperator, column: int) -> str:
    kind = operator.subjects[column][0]
    return "component" if kind == "component" else "device"


def _is_point(bounds: Bounds) -> bool:
    """Whether ``bounds`` is a justified point interval (identified value)."""
    return (
        bounds.lower_w is not None
        and bounds.upper_w is not None
        and bounds.lower_w == bounds.upper_w
    )


def _identifiability_score(
    report: IdentifiabilityReport, subject_id: str
) -> float | None:
    share = report.null_space_share.get(subject_id)
    if share is None:
        return None
    return float(min(1.0, max(0.0, 1.0 - share)))
