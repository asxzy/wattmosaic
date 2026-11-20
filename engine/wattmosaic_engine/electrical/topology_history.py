"""Learn topology from recorded history (the "Discover" button).

The live T5 ledger only sees events after the engine started, so a fresh install
had to wait hours for its first edge.  Recorded power history (HA's recorder)
holds the same evidence: this module replays it through the *same* ledger.

* A **clean step** of a reader is a jump between consecutive samples whose
  levels just before and just after are stable, larger than the reader's own
  robust noise.
* Clean steps whose first post-step samples lie within :data:`GROUP_WINDOW_S`
  of each other form one **group** (design §5 T5, linking v2 W2).
* Every reader is measured across a group exactly like the live runtime does
  (``core.runtime`` step-anchored measurement): the median of its samples in the
  45 s before the group's earliest pre-sample versus the 45 s after its latest
  post-sample.  HA's recorder writes a row only when a value changes, so the
  held value is the "before" level of a quiet reporter however old it is
  (:data:`HOLD_LOOKBACK_S` is ``None``: unlimited).
* Per candidate parent the group is classified with the step-counting rules of
  ``docs/specs/topology-linking-v2/W2-integration.md``: one stepping candidate
  keeps the ledger's own rule (``topology_inference._classify_step``); several
  use explain-away and multi-child matching (:mod:`.topology_explain`, M1) with
  the parent's CONFIRMED children as the known part.  The two legs of a 240 V
  load (M2 ``paired_legs``) stepping together are ONE candidate (their mean
  delta) credited to both legs, never summed into one parent; a duplicate meter
  (M4 ``duplicate_meters``) is never counted as a child of its twin.
* The counts follow ``observe_group``'s bookkeeping in the order things
  happened: a pair exists from its first support, and only an existing pair
  collects conflict or parent-only steps.  Edges confirmed by a first counting
  pass become known children for a second pass (explain-away needs them).  The
  learned edges are then settled by :meth:`TopologyInferenceView.merge_evidence`,
  so lifecycle and direction rules are the live ones.

Counting in plain dicts instead of folding every step through
``observe_group`` matters: the fold copies the whole ledger per step, and a
week of history (~27k steps) held the GIL for minutes, starving the engine's
event loop until the gateway timed requests out (live, 2026-11-20; ADR-0033).

Pure: no I/O, no clock.  The caller provides the series and merges the learned
ledger into the live one (:meth:`TopologyInferenceView.merge_evidence`).
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from wattmosaic_engine.contracts import EdgeLifecycleV3, RelationType, TopologyEdgeV3

from .topology_capacity import LEG_MIN_ACTIVE_SAMPLES, LEG_MIN_ACTIVE_W, LEG_MIN_CORRELATION, paired_legs
from .topology_explain import (
    ChildStep,
    _explainable,
    classify_with_explanation,
    residual_after_known,
    verify_known,
)
from .topology_inference import (
    _DORMANT,
    _SIGMA_FACTOR,
    MATCH_RELATIVE_TOLERANCE,
    TopologyEvidence,
    TopologyInferenceView,
    _classify_step,
    _relation,
)
from .topology_regression import DUP_MIN_ACTIVE_SAMPLES, DUP_MIN_CORRELATION, duplicate_meters

__all__ = [
    "GROUP_WINDOW_S",
    "HistoryReplayStats",
    "MeterLinks",
    "clean_steps",
    "learn_topology_from_history",
    "meter_links",
    "robust_sigma",
]

#: Median windows either side of a step (matches the runtime's ``_T5_ANCHOR_S``).
ANCHOR_S = 45.0
#: A quiet reporter's held value counts as its "before" level within this span.
#: ``None`` = unlimited: recorder rows are change-only, and a 600 s cap dropped
#: 60 % of the sparse readers (live, 2026-11-20).
HOLD_LOOKBACK_S: float | None = None
#: A step must exceed this many robust sigmas of its own reader, and this floor.
STEP_SIGMA_FACTOR = 5.0
MIN_STEP_W = 20.0
#: The samples either side of a step may vary by at most this share of the step.
STABLE_FRACTION = 0.25
SIGMA_FLOOR_W = 2.0
#: Clean steps whose post-step samples lie within this span form one group.
GROUP_WINDOW_S = 20.0
#: Grid of the decimated series that 240 V pairs and duplicate meters are
#: detected on, and the most grid points it may have (a longer lookback widens
#: the step instead).
LINK_GRID_S = 60.0
LINK_GRID_MAX_POINTS = 20_160
#: The vectorised pre-screen admits pairs this far below the exact thresholds,
#: so the exact (M2/M4) check alone decides.
_PRESCREEN_MARGIN = 0.02


@dataclass(frozen=True)
class HistoryReplayStats:
    readers: int
    samples: int
    steps: int
    groups: int = 0
    paired_legs: int = 0
    duplicates: int = 0
    passes: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "readers": self.readers,
            "samples": self.samples,
            "steps": self.steps,
            "groups": self.groups,
            "paired_legs": self.paired_legs,
            "duplicates": self.duplicates,
            "passes": self.passes,
        }


@dataclass(frozen=True)
class _Series:
    times: list[datetime]
    values: list[float]

    def window(self, start: datetime, end: datetime) -> list[float]:
        lo = bisect.bisect_left(self.times, start)
        hi = bisect.bisect_right(self.times, end)
        return self.values[lo:hi]

    def held_before(self, at: datetime, lookback_s: float | None) -> float | None:
        index = bisect.bisect_right(self.times, at) - 1
        if index < 0:
            return None
        if lookback_s is not None and (at - self.times[index]).total_seconds() > lookback_s:
            return None
        return self.values[index]


def _series(rows: Sequence[tuple[datetime, float | None]]) -> _Series:
    present = [(t, v) for t, v in rows if v is not None]
    values = np.asarray([v for _t, v in present], dtype=float)
    finite = np.isfinite(values)
    if not finite.all():
        present = [row for row, ok in zip(present, finite.tolist(), strict=True) if ok]
        values = values[finite]
    times = [t for t, _v in present]
    if any(later < earlier for earlier, later in zip(times, times[1:], strict=False)):
        ordered = sorted((t, float(v)) for t, v in present)
        return _Series([t for t, _ in ordered], [v for _, v in ordered])
    return _Series(times, values.tolist())


def robust_sigma(values: Sequence[float]) -> float:
    """Noise sigma from consecutive differences (MAD), floored at 2 W.

    The median absolute difference ignores the reader's real steps, so a busy
    feed is not judged by its load changes.
    """
    if len(values) < 2:
        return SIGMA_FLOOR_W
    diffs = np.abs(np.diff(np.asarray(values, dtype=float)))
    return max(1.4826 * float(np.median(diffs)) / 2**0.5, SIGMA_FLOOR_W)


def clean_steps(
    times: Sequence[datetime], values: Sequence[float], sigma: float
) -> list[tuple[datetime, datetime, float]]:
    """``(last pre-step sample, first post-step sample, delta)`` per clean step."""
    threshold = max(MIN_STEP_W, STEP_SIGMA_FACTOR * sigma)
    out: list[tuple[datetime, datetime, float]] = []
    for k in range(2, len(values) - 1):
        jump = values[k] - values[k - 1]
        if abs(jump) < threshold:
            continue
        pre, post = values[k - 2 : k], values[k : k + 2]
        allowed = STABLE_FRACTION * abs(jump)
        if max(pre) - min(pre) > allowed or max(post) - min(post) > allowed:
            continue
        delta = statistics.median(post) - statistics.median(pre)
        out.append((times[k - 1], times[k], float(delta)))
    return out


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _anchored_change(series: _Series, lo: datetime, hi: datetime) -> float | None:
    span = timedelta(seconds=ANCHOR_S)
    post = series.window(hi, hi + span)
    if not post:
        return None
    pre = series.window(lo - span, lo)
    if not pre:
        held = series.held_before(lo, HOLD_LOOKBACK_S)
        if held is None:
            return None
        pre = [held]
    return float(_median(post) - _median(pre))


# --------------------------------------------------------------------------- #
# 240 V pairs and duplicate meters on a decimated grid                          #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MeterLinks:
    """Readers that move together for wiring reasons, not containment.

    ``pairs``: the two legs of one 240 V load (M2 ``paired_legs``).
    ``duplicates``: one wire measured twice (M4 ``duplicate_meters``).
    """

    pairs: tuple[tuple[str, str], ...] = ()
    duplicates: tuple[tuple[str, str], ...] = ()
    #: reader -> its duplicate meters (never its children, never its parent)
    twins: Mapping[str, frozenset[str]] = field(default_factory=dict)
    #: couples collapsed into one candidate when both step together
    couples: tuple[tuple[str, str], ...] = ()

    @property
    def coupled(self) -> frozenset[str]:
        return frozenset(reader for couple in self.couples for reader in couple)


def _decimate(series: Mapping[str, _Series], step_s: float) -> tuple[list[str], np.ndarray]:
    """Hold-resample every reader onto one grid (no hold cap; NaN = absent)."""
    names = sorted(series)
    if not names:
        return names, np.zeros((0, 0))
    start = min(series[name].times[0] for name in names)
    end = max(series[name].times[-1] for name in names)
    span = (end - start).total_seconds()
    step = max(step_s, span / LINK_GRID_MAX_POINTS)
    count = int(span // step) + 1
    grid = [start + timedelta(seconds=i * step) for i in range(count)]
    matrix = np.full((len(names), count), np.nan)
    for row, name in enumerate(names):
        s = series[name]
        idx = np.fromiter(
            (bisect.bisect_right(s.times, instant) - 1 for instant in grid), dtype=np.int64, count=count
        )
        values = np.asarray(s.values, dtype=float)
        present = idx >= 0
        matrix[row, present] = values[idx[present]]
    return names, matrix


def _pairwise(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pairwise-complete Pearson ``r`` and comparable counts of every row pair."""
    mask = np.isfinite(matrix).astype(float)
    with np.errstate(all="ignore"):
        means = np.nanmean(np.where(mask > 0, matrix, np.nan), axis=1)
    means = np.nan_to_num(means)
    centred = np.where(mask > 0, matrix - means[:, None], 0.0)
    n = mask @ mask.T
    sx = centred @ mask.T
    sxx = (centred**2) @ mask.T
    sxy = centred @ centred.T
    with np.errstate(all="ignore"):
        cov = n * sxy - sx * sx.T
        var = n * sxx - sx**2
        r = cov / np.sqrt(var * var.T)
    return np.nan_to_num(r, nan=-1.0), n


def _rows(matrix: np.ndarray, names: Sequence[str], keep: Iterable[str]) -> dict[str, list[float | None]]:
    index = {name: i for i, name in enumerate(names)}
    return {
        name: [None if math.isnan(v) else float(v) for v in matrix[index[name]].tolist()]
        for name in sorted(set(keep))
    }


def _screened(r: np.ndarray, ok: np.ndarray, names: Sequence[str]) -> set[str]:
    """Readers in any pair the pre-screen admits (upper triangle only)."""
    hits = np.argwhere(np.triu(ok & (r > -1.0), k=1))
    return {names[i] for pair in hits for i in pair}


def _meter_links(names: Sequence[str], matrix: np.ndarray) -> MeterLinks:
    if len(names) < 2:
        return MeterLinks()
    # Pre-screen with one vectorised pass, then let the exact M2/M4 functions
    # decide on the few readers that passed: their pure-Python pairwise loops
    # over a week x 46 readers would cost seconds of GIL (ADR-0033).
    magnitude = np.abs(matrix)
    r_abs, _n = _pairwise(magnitude)
    active = np.nan_to_num(magnitude) >= LEG_MIN_ACTIVE_W
    active_count = active.astype(float) @ active.T.astype(float)
    leg_ok = (active_count >= LEG_MIN_ACTIVE_SAMPLES) & (r_abs >= LEG_MIN_CORRELATION - _PRESCREEN_MARGIN)
    leg_readers = _screened(r_abs, leg_ok, names)
    r_raw, n_raw = _pairwise(matrix)
    dup_ok = (n_raw >= DUP_MIN_ACTIVE_SAMPLES) & (r_raw >= DUP_MIN_CORRELATION - _PRESCREEN_MARGIN)
    dup_readers = _screened(r_raw, dup_ok, names)

    pairs = (
        tuple((a, b) for a, b, _r in paired_legs(_rows(matrix, names, leg_readers)))
        if len(leg_readers) >= 2
        else ()
    )
    duplicates = (
        tuple((a, b) for a, b, _r, _o in duplicate_meters(_rows(matrix, names, dup_readers)))
        if len(dup_readers) >= 2
        else ()
    )
    twins: dict[str, set[str]] = {}
    for a, b in duplicates:
        twins.setdefault(a, set()).add(b)
        twins.setdefault(b, set()).add(a)
    return MeterLinks(
        pairs=pairs,
        duplicates=duplicates,
        twins={name: frozenset(values) for name, values in twins.items()},
        couples=tuple(sorted({*pairs, *duplicates})),
    )


def meter_links(history: Mapping[str, Sequence[tuple[datetime, float | None]]]) -> MeterLinks:
    """240 V pairs and duplicate meters of ``history`` (60 s decimated grid)."""
    series = {reader: _series(rows) for reader, rows in history.items()}
    return _meter_links(
        *_decimate({reader: s for reader, s in series.items() if len(s.values) >= 4}, LINK_GRID_S)
    )


def _grid_sigmas(names: Sequence[str], matrix: np.ndarray) -> dict[str, float]:
    """``robust_sigma`` of every reader on the hold-resampled grid.

    Recorder rows are change-only: every consecutive pair of rows differs by a
    real change, so the raw estimate is the size of the reader's loads (live: a
    sparse plug read sigma ~500 W and never "moved").  On a hold grid its flat
    stretches repeat the held value and the estimate is its noise again.
    """
    out: dict[str, float] = {}
    for row, name in enumerate(names):
        values = matrix[row]
        diffs = np.abs(np.diff(values[np.isfinite(values)]))
        if diffs.size:
            out[name] = max(1.4826 * float(np.median(diffs)) / 2**0.5, SIGMA_FLOOR_W)
    return out


# --------------------------------------------------------------------------- #
# Group counting                                                              #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Group:
    lo: datetime
    hi: datetime
    #: stepping reader -> (its own first post-step sample, delta)
    members: Mapping[str, tuple[datetime, float]]
    #: stepping reader -> the cause id of its step
    causes: Mapping[str, str]


def _groups(events: Sequence[tuple[datetime, str, datetime, float]]) -> list[_Group]:
    window = timedelta(seconds=GROUP_WINDOW_S)
    out: list[_Group] = []
    current: list[tuple[datetime, str, datetime, float]] = []

    def close() -> None:
        members: dict[str, tuple[datetime, float]] = {}
        for hi, reader, _lo, delta in current:
            # Two steps of one reader inside a group: its net change counts.
            first, total = members.get(reader, (hi, 0.0))
            members[reader] = (first, total + delta)
        out.append(
            _Group(
                lo=min(e[2] for e in current),
                hi=max(e[0] for e in current),
                members=members,
                causes={
                    reader: f"hist:{reader}:{first.isoformat()}"
                    for reader, (first, _delta) in members.items()
                },
            )
        )

    for event in events:
        if current and event[0] - current[0][0] > window:
            close()
            current = []
        current.append(event)
    if current:
        close()
    return out


def _closure(edges: Iterable[tuple[str, str]]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """``(ancestors, descendants)`` per reader through the given parent->child pairs."""
    children: dict[str, set[str]] = {}
    for parent, child in edges:
        if parent != child:
            children.setdefault(parent, set()).add(child)
    descendants: dict[str, set[str]] = {}
    for root in children:
        seen: set[str] = set()
        stack = list(children[root])
        while stack:
            node = stack.pop()
            if node in seen or node == root:
                continue
            seen.add(node)
            stack.extend(children.get(node, ()))
        descendants[root] = seen
    ancestors: dict[str, set[str]] = {}
    for root, below in descendants.items():
        for node in below:
            ancestors.setdefault(node, set()).add(root)
    return ancestors, descendants


@dataclass
class _Context:
    sigmas: Mapping[str, float]
    authority: frozenset[tuple[str, str]]
    links: MeterLinks
    known: frozenset[tuple[str, str]] = frozenset()
    ancestors: Mapping[str, set[str]] = field(default_factory=dict)
    descendants: Mapping[str, set[str]] = field(default_factory=dict)
    known_parents: frozenset[str] = frozenset()

    def ancestors_matter(self, parent: str, move: float, above: set[str]) -> bool:
        """Whether excluding ``above`` (stepping ancestors) changes any verdict.

        For a flat parent it does not: an excluded ancestor still gets the
        lone-step conflict, which is exactly the flat branch of the matching --
        unless the ancestor would have been collapsed with a coupled reader.
        """
        if abs(move) > _SIGMA_FACTOR * self.sigmas[parent]:
            return True
        return not above.isdisjoint(self.links.coupled)

    def with_known(self, known: frozenset[tuple[str, str]]) -> _Context:
        ancestors, descendants = _closure(known)
        return replace(
            self,
            known=known,
            ancestors=ancestors,
            descendants=descendants,
            known_parents=frozenset(parent for parent, _child in known),
        )


#: One counting operation: ``(verdict, parent, child, cause)``, verdict
#: ``"support"`` or ``"conflict"``.
_Op = tuple[str, str, str, str]


def _supports_alone(move: float, sigma_p: float, step: ChildStep) -> bool:
    """``_classify_step`` support band, inlined for the hot loop."""
    if abs(move) <= _SIGMA_FACTOR * sigma_p:
        return False
    tolerance = _SIGMA_FACTOR * math.sqrt(sigma_p**2 + step.sigma**2)
    tolerance += MATCH_RELATIVE_TOLERANCE * abs(step.delta)
    return abs(move - step.delta) <= tolerance


def _collapse(
    ids: Sequence[str], group: _Group, ctx: _Context
) -> tuple[list[ChildStep], dict[str, tuple[str, ...]]]:
    """Child steps of ``ids`` with every stepping couple collapsed.

    The two legs of one 240 V load (or one wire measured twice) are ONE
    candidate: the mean delta and one leg's sigma, credited to both legs --
    never summed into one parent (W2 "Step counting").
    """
    members, sigmas = group.members, ctx.sigmas
    steps: list[ChildStep] = []
    credit: dict[str, tuple[str, ...]] = {}
    left = set(ids)
    if len(left) > 1:
        for a, b in ctx.links.couples:
            if a in left and b in left:
                left -= {a, b}
                key = f"{a}+{b}"
                delta = (members[a][1] + members[b][1]) / 2.0
                steps.append(ChildStep(key, delta, max(sigmas[a], sigmas[b])))
                credit[key] = (a, b)
    for c in ids:
        if c in left:
            steps.append(ChildStep(c, members[c][1], sigmas[c]))
            credit[c] = (c,)
    return steps, credit


def _verdicts(
    move: float, sigma_p: float, known: list[ChildStep], unknown: list[ChildStep]
) -> dict[str, str | None]:
    """Verdict per candidate (known or unknown) of one parent move."""
    verdicts: dict[str, str | None] = {}
    if len(known) + len(unknown) == 1:
        # One stepping candidate: today's exact rule.
        only = (known or unknown)[0]
        verdicts[only.reader_id] = _classify_step(
            move, only.delta, sigma_p=sigma_p, sigma_c=only.sigma
        )
        return verdicts
    if known:
        verdicts.update(verify_known(move, sigma_p, known=known, unknown=unknown))
        for k in known:
            if verdicts.get(k.reader_id) is not None:
                continue
            # As for the unknown: a known child that alone explains what its
            # siblings leave is supported, even when an unrelated equal step
            # makes M1's subset match ambiguous (both mains stepping with a
            # 240 V load).
            others = [o for o in known if o is not k]
            rest, rest_sigma = residual_after_known(
                move, sigma_p, _explainable(move, others)
            )
            if _supports_alone(rest, rest_sigma, k):
                verdicts[k.reader_id] = "support"
    if not unknown:
        return verdicts
    residual = move - sum(k.delta for k in known)
    residual_sigma = math.sqrt(sigma_p**2 + sum(k.sigma**2 for k in known))
    flat_band = _SIGMA_FACTOR * residual_sigma
    if abs(residual) <= flat_band:
        if known:
            verdicts.update(classify_with_explanation(move, sigma_p, known=known, unknown=unknown))
            return verdicts
        # classify_with_explanation's flat branch, inlined for the hot loop
        # (identical: there is nothing to explain away).
        for u in unknown:
            if abs(u.delta) <= flat_band:
                continue
            tolerance = _SIGMA_FACTOR * math.sqrt(residual_sigma**2 + u.sigma**2)
            tolerance += MATCH_RELATIVE_TOLERANCE * abs(u.delta)
            if abs(residual - u.delta) > tolerance:
                verdicts[u.reader_id] = "conflict"
        return verdicts
    singles = [u for u in unknown if _supports_alone(residual, residual_sigma, u)]
    if singles:
        # A candidate that explains the remainder alone is supported exactly as
        # a lone step would be; subsets are for remainders no single child
        # explains (example 2: P +100 = Y +30 + Z +70).
        verdicts.update({u.reader_id: "support" for u in singles})
    else:
        verdicts.update(classify_with_explanation(move, sigma_p, known=known, unknown=unknown))
    return verdicts


def _emit(
    ops: list[_Op],
    parent: str,
    verdicts: Mapping[str, str | None],
    credit: Mapping[str, tuple[str, ...]],
    group: _Group,
    ctx: _Context,
) -> list[_Op]:
    for key, verdict in verdicts.items():
        if verdict is None:
            continue
        for child in credit[key]:
            if (parent, child) not in ctx.authority:
                ops.append((verdict, parent, child, group.causes[child]))
    return ops


def _parent_ops(group: _Group, parent: str, move: float, ctx: _Context) -> list[_Op]:
    """Verdicts for every stepping candidate child of ``parent`` in ``group``."""
    twins = ctx.links.twins.get(parent, frozenset())
    candidates = [c for c in group.members if c != parent and c not in twins]
    if not candidates:
        return []
    sigma_p = ctx.sigmas[parent]
    ops: list[_Op] = []
    if len(candidates) == 1:
        steps, credit = _collapse(candidates, group, ctx)
        return _emit(ops, parent, _verdicts(move, sigma_p, [], steps), credit, group, ctx)
    # Several: the parent's own ancestors are never matched as its children
    # (W2 "Step counting").  A flat parent under a clearly stepping ancestor is
    # still the lone-step conflict, though: that conflict on the reverse edge
    # is what lets the direction rule confirm the forward one.
    above = ctx.ancestors.get(parent, ())
    for c in candidates:
        if c in above and (parent, c) not in ctx.authority:
            verdict = _classify_step(
                move, group.members[c][1], sigma_p=sigma_p, sigma_c=ctx.sigmas[c]
            )
            if verdict == "conflict":
                ops.append((verdict, parent, c, group.causes[c]))
    candidates = [c for c in candidates if c not in above]
    if not candidates:
        return ops
    known_ids = [c for c in candidates if (parent, c) in ctx.known]
    # A descendant of a stepping known child is explained by that child: it is
    # neither a known nor an unknown child of this parent here.
    below: set[str] = set()
    for k in known_ids:
        below |= ctx.descendants.get(k, set())
    unknown_ids = [c for c in candidates if c not in known_ids and c not in below]
    # A couple stays ONE candidate on the known side too: two known legs of one
    # 240 V load would otherwise explain each other away (one leg's watts on
    # this parent, never both).  A couple with a known leg is known.
    steps, credit = _collapse([*known_ids, *unknown_ids], group, ctx)
    known_set = set(known_ids)
    known = [u for u in steps if not known_set.isdisjoint(credit[u.reader_id])]
    unknown = [u for u in steps if known_set.isdisjoint(credit[u.reader_id])]
    return _emit(ops, parent, _verdicts(move, sigma_p, known, unknown), credit, group, ctx)


def _group_ops(
    group: _Group, changes: Mapping[str, float], ctx: _Context, parents: Iterable[str]
) -> dict[str, list[_Op]]:
    """Non-empty verdict lists per candidate parent (empty when recounting)."""
    out: dict[str, list[_Op]] = {}
    twins = ctx.links.twins
    if len(group.members) == 1:
        # The common case, kept tight: one stepping reader, today's exact rule
        # for every other measurable reader as its candidate parent.
        # ``_classify_step`` (beta 1) is inlined: this loop runs once per
        # reader per step of a week of history (ADR-0033).
        ((child, (_at, delta)),) = group.members.items()
        var_c = ctx.sigmas[child] ** 2
        relative = MATCH_RELATIVE_TOLERANCE * abs(delta)
        cause = group.causes[child]
        sigmas, authority = ctx.sigmas, ctx.authority
        for parent in parents:
            move = changes.get(parent)
            if move is None or parent == child:
                continue
            sigma_p = sigmas[parent]
            flat_band = _SIGMA_FACTOR * sigma_p
            tolerance = _SIGMA_FACTOR * math.sqrt(sigma_p * sigma_p + var_c) + relative
            moved = abs(move) > flat_band
            if moved and abs(move - delta) <= tolerance:
                verdict = "support"
            elif not moved and abs(delta) > flat_band and abs(move - delta) > tolerance:
                verdict = "conflict"
            else:
                continue
            if (parent, child) in authority or (twins and child in twins.get(parent, ())):
                continue
            out[parent] = [(verdict, parent, child, cause)]
        return out
    # Several stepping readers.  A parent that is not a member, has no twin,
    # known child or ancestor among them sees the same collapsed candidates:
    # build them once per group.
    members = group.members
    shared: tuple[list[ChildStep], dict[str, tuple[str, ...]]] | None = None
    ancestors, known_parents = ctx.ancestors, ctx.known_parents
    for parent in parents:
        move = changes.get(parent)
        if move is None:
            continue
        above = ancestors.get(parent)
        plain = (
            parent not in members
            and parent not in twins
            and (parent not in known_parents or not any((parent, c) in ctx.known for c in members))
            and (
                above is None
                or above.isdisjoint(members)
                or not ctx.ancestors_matter(parent, move, above & members.keys())
            )
        )
        if plain:
            if shared is None:
                shared = _collapse(list(members), group, ctx)
            steps, credit = shared
            sigma_p = ctx.sigmas[parent]
            flat_band = _SIGMA_FACTOR * sigma_p
            verdicts: dict[str, str | None] = {}
            # ``_verdicts`` without known children, inlined (ADR-0033).
            if abs(move) <= flat_band:
                for u in steps:
                    if abs(u.delta) > flat_band and abs(move - u.delta) > (
                        _SIGMA_FACTOR * math.sqrt(sigma_p * sigma_p + u.sigma * u.sigma)
                        + MATCH_RELATIVE_TOLERANCE * abs(u.delta)
                    ):
                        verdicts[u.reader_id] = "conflict"
            else:
                for u in steps:
                    if _supports_alone(move, sigma_p, u):
                        verdicts[u.reader_id] = "support"
                if not verdicts and len(steps) > 1:
                    verdicts = classify_with_explanation(move, sigma_p, known=[], unknown=steps)
            ops = _emit([], parent, verdicts, credit, group, ctx) if verdicts else []
        else:
            ops = _parent_ops(group, parent, move, ctx)
        if ops:
            out[parent] = ops
    return out


def _flat_readers(group: _Group, changes: Mapping[str, float], ctx: _Context) -> tuple[str, ...]:
    """Readers that stayed flat across ``group``: every stepping reader as their
    parent is a parent-only step (neutral residual evidence, AT-02)."""
    return tuple(
        reader
        for reader, change in changes.items()
        if reader not in group.members and abs(change) <= _SIGMA_FACTOR * ctx.sigmas[reader]
    )


@dataclass
class _Counts:
    support: dict[tuple[str, str], int] = field(default_factory=dict)
    conflict: dict[tuple[str, str], int] = field(default_factory=dict)
    parent_only: dict[tuple[str, str], int] = field(default_factory=dict)
    causes: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    first_at: dict[tuple[str, str], datetime] = field(default_factory=dict)
    last_support: dict[tuple[str, str], datetime] = field(default_factory=dict)
    last_conflict: dict[tuple[str, str], datetime] = field(default_factory=dict)

    def apply(self, at: datetime, ops: Iterable[_Op]) -> None:
        for kind, parent, child, cause in ops:
            key = (parent, child)
            if kind == "support":
                self.support[key] = self.support.get(key, 0) + 1
                self.causes.setdefault(key, set()).add(cause)
                self.first_at.setdefault(key, at)
                self.last_support[key] = at
            elif kind == "conflict" and key in self.support:
                # As in observe_group: a pair exists from its first support,
                # and only an existing pair collects conflict.
                self.conflict[key] = self.conflict.get(key, 0) + 1
                self.causes[key].add(cause)
                self.last_conflict[key] = at

    def apply_parent_only(self, stepping: Iterable[str], flat: Sequence[str]) -> None:
        for parent in stepping:
            for child in flat:
                key = (parent, child)
                if key in self.support:
                    self.parent_only[key] = self.parent_only.get(key, 0) + 1


def _count(
    groups: Sequence[_Group],
    ops: Sequence[tuple[Mapping[str, list[_Op]], tuple[str, ...]]],
) -> _Counts:
    counts = _Counts()
    for group, (by_parent, flat) in zip(groups, ops, strict=True):
        for parent_ops in by_parent.values():
            counts.apply(group.hi, parent_ops)
        counts.apply_parent_only(group.members, flat)
    return counts


def _learned_view(view: TopologyInferenceView, counts: _Counts) -> TopologyInferenceView:
    seeded = {(e.source_endpoint_id, e.target_endpoint_id) for e in view.edges}
    learned = tuple(
        TopologyEdgeV3(
            edge_id=f"te-inf::{parent}::{child}",
            relation=RelationType.CONTAINS,
            source_endpoint_id=parent,
            target_endpoint_id=child,
            lifecycle=EdgeLifecycleV3.CANDIDATE,
            declared_by="inference",
            beta=1.0,
            beta_basis="unity",
            evidence=TopologyEvidence(
                support=count,
                conflict=counts.conflict.get((parent, child), 0),
                parent_only=counts.parent_only.get((parent, child), 0),
                independent_causes=len(counts.causes[(parent, child)]),
                last_support_at=counts.last_support.get((parent, child)),
                last_conflict_at=counts.last_conflict.get((parent, child)),
            ),
            valid_from=counts.first_at[(parent, child)],
            pinned=False,
        )
        for (parent, child), count in sorted(counts.support.items())
        if (parent, child) not in seeded
    )
    return replace(
        view,
        edges=(*view.edges, *learned),
        causes_by_edge={
            **view.causes_by_edge,
            **{(p, c, "contains"): frozenset(values) for (p, c), values in counts.causes.items()},
        },
    )


def _confirmed(view: TopologyInferenceView) -> frozenset[tuple[str, str]]:
    return frozenset(
        (e.source_endpoint_id, e.target_endpoint_id)
        for e in view.edges
        if _relation(e.relation) == "contains" and e.lifecycle == EdgeLifecycleV3.CONFIRMED
    )


def learn_topology_from_history(
    view: TopologyInferenceView,
    history: Mapping[str, Sequence[tuple[datetime, float | None]]],
    *,
    known: Iterable[tuple[str, str]] = (),
    levels: Mapping[str, float] | None = None,
) -> tuple[TopologyInferenceView, HistoryReplayStats]:
    """Count every clean-step group in ``history`` (reader -> samples) per pair.

    ``view`` is normally an empty ledger seeded with the user's pinned,
    declared and rejected pairs: those pairs are never counted (user authority).
    ``known`` are parent->child pairs already CONFIRMED in the live ledger: the
    known children explained away when several children step together.  The
    seed's own CONFIRMED CONTAINS edges are known too.  ``levels`` (median
    absolute watts per reader) settle the first pass's scratch ledger; they are
    computed from ``history`` when omitted.

    Returns the seed plus CANDIDATE edges carrying the counted evidence;
    :meth:`TopologyInferenceView.merge_evidence` settles their lifecycles.
    """
    series = {reader: _series(rows) for reader, rows in history.items()}
    series = {reader: s for reader, s in series.items() if len(s.values) >= 4}
    raw_sigmas = {reader: robust_sigma(s.values) for reader, s in series.items()}
    events: list[tuple[datetime, str, datetime, float]] = []
    for reader, s in series.items():
        for lo, hi, delta in clean_steps(s.times, s.values, raw_sigmas[reader]):
            events.append((hi, reader, lo, delta))
    events.sort()
    groups = _groups(events)
    names, matrix = _decimate(series, LINK_GRID_S)
    links = _meter_links(names, matrix)
    # Classification noise: the smaller of the raw and the hold-grid estimate,
    # so a change-only reader is judged by its noise, not by its loads.
    grid_sigmas = _grid_sigmas(names, matrix)
    sigmas = {
        reader: min(sigma, grid_sigmas.get(reader, sigma)) for reader, sigma in raw_sigmas.items()
    }

    authority = frozenset(
        (e.source_endpoint_id, e.target_endpoint_id)
        for e in view.edges
        if e.pinned or e.declared_by == "user" or e.lifecycle in _DORMANT
    )
    known0 = frozenset(known) | _confirmed(view)
    ctx = _Context(sigmas=sigmas, authority=authority, links=links).with_known(known0)

    # Pass 1: measure every reader across every group once.  Only groups with
    # several stepping readers depend on the known children, so only their
    # measurements are kept for a second pass.
    ops: list[tuple[dict[str, list[_Op]], tuple[str, ...]]] = []
    multi: dict[int, dict[str, float]] = {}
    for index, group in enumerate(groups):
        changes: dict[str, float] = {}
        for reader, s in series.items():
            change = _anchored_change(s, group.lo, group.hi)
            if change is not None:  # not measurable across this group: no evidence
                changes[reader] = change
        ops.append((_group_ops(group, changes, ctx, changes), _flat_readers(group, changes, ctx)))
        if len(group.members) > 1:
            multi[index] = changes
    counts = _count(groups, ops)
    passes = 1

    # Pass 2: edges the first pass confirms are known children for explain-away.
    if multi:
        if levels is None:
            levels = {
                reader: float(np.median(np.abs(np.asarray(s.values, dtype=float))))
                for reader, s in series.items()
            }
        scratch = replace(view, versions=None).merge_evidence(
            _learned_view(view, counts), levels=levels
        )
        known1 = known0 | _confirmed(scratch)
        if known1 != known0:
            ctx2 = ctx.with_known(known1)
            new = known1 - known0
            # Only these parents can see different verdicts in pass 2: new known
            # children, known children whose descendants changed, or changed
            # ancestors.
            changed_desc = {
                c
                for c in set(ctx.descendants) | set(ctx2.descendants)
                if ctx.descendants.get(c, set()) != ctx2.descendants.get(c, set())
            }
            candidates_2 = sorted(
                {p for p, _c in new}
                | {p for p, c in known1 if c in changed_desc}
                | {
                    p
                    for p in set(ctx.ancestors) | set(ctx2.ancestors)
                    if ctx.ancestors.get(p, set()) != ctx2.ancestors.get(p, set())
                }
            )
            for index, changes in multi.items():
                group = groups[index]
                members = set(group.members)
                affected = [
                    parent
                    for parent in candidates_2
                    # ``parent in changes`` guards BOTH alternatives: the
                    # ancestor test reads ``changes[parent]``.
                    if parent in changes
                    and (
                        any(
                            (parent, c) in new
                            or (
                                (parent, c) in known1
                                and (ctx.descendants.get(c, set()) & members)
                                != (ctx2.descendants.get(c, set()) & members)
                            )
                            for c in members
                        )
                        or (
                            (ctx.ancestors.get(parent, set()) & members)
                            != (ctx2.ancestors.get(parent, set()) & members)
                            and ctx2.ancestors_matter(
                                parent,
                                changes[parent],
                                (ctx.ancestors.get(parent, set()) ^ ctx2.ancestors.get(parent, set()))
                                & members,
                            )
                        )
                    )
                ]
                if affected:
                    by_parent = ops[index][0]
                    for parent in affected:
                        by_parent.pop(parent, None)
                    by_parent.update(_group_ops(group, changes, ctx2, affected))
            counts = _count(groups, ops)
            passes = 2

    result = _learned_view(view, counts)
    stats = HistoryReplayStats(
        readers=len(series),
        samples=sum(len(s.values) for s in series.values()),
        steps=len(events),
        groups=len(groups),
        paired_legs=len(links.pairs),
        duplicates=len(links.duplicates),
        passes=passes,
    )
    return result, stats
