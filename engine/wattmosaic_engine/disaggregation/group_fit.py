"""Tier B: what a *group* of co-toggling fixtures draws, and nothing more.

Why this module exists
----------------------
:mod:`wattmosaic_engine.ingestion.correlation` attributes a fixture to a circuit
by pairing its on/off edges with measured steps on that row.  That works for a
fixture that switches on its own.  It cannot work for the 67 % of real
transitions that fire a scene: one physical event switches several loads on one
circuit, the CT sees a single sum, and no arithmetic can split that sum into its
parts.  Step pairing then either rejects the fixtures (correct) or credits each
of them the whole step (double counting).

The honest alternative is to stop pretending the individuals are separable.  Fit
the *aggregate* energy balance of the row,

    row_w(t) ~ base + Σ_g w_g · on_g(t),        w_g ≥ 0

where each ``g`` is a co-toggle group rather than a fixture, and report the
group total with ``individually_identifiable=False``.  Two numbers that are true
are worth more than six that are not: on the production install, collapsing the
`lights_ct2` scene into group regressors costs ``0.003`` R² (0.7912 per-light to
0.7880 grouped) and replaces six contradictory ~500 W "fixtures" with two
measured groups, 363.8 W and 325.6 W (report Q3.4).

Why a rotated null, and why it is the gate
-------------------------------------------
R² against a constant baseline is not evidence.  A light that is on 60 % of the
time is a duty-cycle regressor: it will "explain" any row that is also busier
during the evening, with no causal link at all.  So each fit is compared against
the same fit on the whole on/off matrix rotated by a large offset.  A rotation
keeps every light's duty cycle, its ON-duration distribution, the autocorrelation
of the series and the joint scene structure; it destroys only the alignment with
the row.  R² that survives that is attributable to *switching*; R² that does not
is attributable to *duty*.

An independent per-bin shuffle is **not** a valid null here, and the difference
is decisive.  It also destroys autocorrelation -- a light that is on for an hour
becomes scattered single bins -- so the shuffled design cannot fit anything and
the null collapses to ≈0.001.  Measured on 48 h of production data, that null
accepted every main feed and every sub-panel CT (margins +0.23 to +0.63), which
is precisely the false conclusion the gate exists to prevent.  Rotation gives a
p95 of 0.68-0.80, and then only the three real lighting rows pass.

Measured on 48 h of production recorder data (15 s bins, 25 active lights).  The
figures below are report **Q3**'s: per-light regressors, the study's own null.
They are the provenance for the thresholds, not a reproduction -- this module
fits *group* regressors and uses a rolling rotation, so on the same window it
measures the higher R² and nulls in the second table.  Both tables reach the
same verdict, which is the point: the thresholds are not sensitive to which of
the two honest implementations computes them.

Report Q3 (per-light NNLS, block-permutation null -- the basis for the
thresholds):

===============  ========  ==========  ==========  ========
row              R²        null p95    margin      verdict
===============  ========  ==========  ==========  ========
``lights_ct1``   0.8301    0.4976      +0.333      accepted
``lights_ct2``   0.7912    0.1447      +0.647      accepted
``lights_ct3``   0.5774    0.4000      +0.177      accepted
===============  ========  ==========  ==========  ========

This module, same 48 h (group regressors, rolling rotation null):

===============  ========  ==========  ==========  ========
row              R²        null p95    margin      verdict
===============  ========  ==========  ==========  ========
``lights_ct1``   0.9784    0.7062      +0.272      accepted
``lights_ct2``   0.9933    0.7320      +0.261      accepted
``lights_ct3``   0.9599    0.7973      +0.163      accepted
``outdoor_plug`` 0.9974    0.7946      +0.203      accepted
===============  ========  ==========  ==========  ========

The four lighting/plug rows clear their nulls by +0.16 to +0.27.  Every main feed
and every sub-panel CT is explained *no better than chance*: on the same run the
best non-lighting row is `main_floor_plug` at +0.05 and the worst is `ev_charger`
at −0.41, with every installation-wide feed between −0.29 and −0.41.  Those rows
are refused rather than reported as weak groups.  The defaults below sit inside
that measured gap: ``0.30`` is above the constant-only R² that duty cycle alone
produces, and ``0.15`` is below the smallest margin every accepted row achieved
(+0.163) while staying far above the largest margin any refused row achieved
(+0.05).

What this module deliberately does not do
-----------------------------------------
* It never splits a group total across its members.  ``estimated_w`` is ``None``
  for every member of a multi-fixture group, and no heuristic fills it in.
* It never emits a group for an installation-wide row.  The caller passes those
  ids in ``excluded_ct_ids``; this module has no topology and must not guess.
* It writes nothing, reads nothing, and talks to nothing.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
from scipy.optimize import nnls

from wattmosaic_engine.ingestion.correlation import (
    CO_TOGGLE_S,
    CO_TOGGLE_SHARE,
    MIN_RESPONSE_W,
    OFF_STATES,
    ON_STATES,
    parse_history_payload,
)

#: Bin width of the common time grid, in seconds.  Wide enough that a
#: change-only channel still lands in most bins, narrow enough that a light which
#: is on for two minutes is not smeared across the whole fit.  This is the same
#: grid the feasibility study measured its R² values on, so the thresholds
#: documented in the module docstring are directly comparable.
BIN_S = 15.0
#: A bin is filled from the last sample before it only while that sample is at
#: most this old.  Beyond it the CT was not reporting, and holding the value
#: forward would invent watts for a period nothing measured.
MAX_GAP_S = 180.0
#: Shuffles used for the null distribution.  50 is the smallest count the
#: contract allows, and the p95 of 50 draws is stable enough to sit 0.15 R² away
#: from every measured real row while adding well under a second to a full run.
N_NULL_PERMUTATIONS = 50
#: Bins skipped at each end of the rotation, so a null draw is a *rotation* of
#: the whole on/off matrix rather than a near-identity or a near-inverse copy of
#: it.  96 bins is one day at 15 s -- the same block size the feasibility study
#: rotated by, and large enough that a draw cannot accidentally realign a scene
#: with the row it drove.
NULL_MIN_SHIFT_BINS = 96
#: Entities considered per fit.  A cap is required because the null costs one
#: solve per draw per row, and the whole install must finish in seconds.
#: Entities are taken in ``entity_id`` order so the cap is deterministic rather
#: than dependent on dictionary insertion order.
MAX_ENTITIES = 200
#: Eigenvalues of the (singular, because scenes are exactly collinear) Gram
#: matrix below this fraction of the largest are dropped before the factorisation
#: used by the fit.  In those directions no coefficient is identifiable, so the
#: solve would be numerically meaningless -- and the null, which reuses the same
#: factor, would report a number manufactured entirely by rounding noise.
_EIGENVALUE_FLOOR = 1e-9


@dataclass(slots=True, frozen=True)
class GroupMember:
    """One fixture in a co-toggling group.

    ``estimated_w`` is ``None`` for every member of a multi-fixture group: the
    data measured their *sum*, and the honest answer to "how much is this one" is
    "not separable".  A singleton group has no sum to split, so it carries the
    aggregate coefficient it was actually fitted with.
    """

    entity_id: str
    estimated_w: float | None


@dataclass(slots=True, frozen=True)
class GroupFit:
    """What an aggregate fit established about one group on one circuit row.

    ``total_w`` is the group's fitted coefficient -- the watts the *group* adds
    when it is on -- never a per-fixture figure.  ``r2_null_p95`` and
    ``r2_margin`` travel with it because a bare R² invites a reader to treat a
    duty-cycle artefact as a measurement.
    """

    ct_id: str
    total_w: float
    r2: float
    r2_null_p95: float
    r2_margin: float
    members: tuple[GroupMember, ...]
    evidence: tuple[str, ...]

    @property
    def individually_identifiable(self) -> bool:
        """True only when a member carries a coefficient the data identified.

        Derived rather than stored: the only thing that can make an individual
        identifiable is a measured ``estimated_w``, so a group whose members are
        all ``None`` cannot claim otherwise by setting a flag.
        """
        return any(member.estimated_w is not None for member in self.members)


def _epoch_pairs(rows: Sequence[Any]) -> list[tuple[float, Any]]:
    """Recorder rows to ``(epoch_seconds, value)``, in recorder order.

    The frozen signature takes ``list[dict]``, which is what the recorder
    returns.  A caller that has already normalised history into
    ``(datetime, value)`` pairs is accepted too, so it is never forced to
    re-serialise into dicts just to cross this boundary.

    Epoch seconds, not ``datetime``, because every consumer here is
    numeric -- binning, differencing, ``searchsorted``.  Converting 810 000
    stamps to ``datetime`` and back cost more than the fits themselves did.

    The common recorder shape -- a ``str`` state and a numeric ``lu`` -- is taken
    directly.  Anything unexpected falls back to :func:`parse_history_payload`,
    so the two paths can never disagree about a shape the generic parser
    understands, and a change in the recorder format degrades to the canonical
    parser rather than to silently dropped history.
    """
    if not rows:
        return []
    if not isinstance(rows[0], Mapping):
        out: list[tuple[float, Any]] = []
        for row in rows:
            if (
                isinstance(row, (tuple, list))
                and len(row) >= 2
                and isinstance(row[0], datetime)
            ):
                out.append((row[0].timestamp(), row[1]))
        return out

    stamps: list[float] = []
    texts: list[str] = []
    append_stamp = stamps.append
    append_text = texts.append
    for row in rows:
        # One ``dict.get`` chain and one ``isinstance`` per row: this loop runs
        # ~800 000 times for a full install and the per-row attribute lookups
        # dominate the run otherwise.
        try:
            raw_stamp = row.get("lu")
            if raw_stamp is None:
                raw_stamp = row.get("last_updated")
            raw_state = row.get("s", row.get("state"))
        except AttributeError:
            return _epoch_pairs_slow(rows)
        if not isinstance(raw_stamp, (int, float)) or raw_state is None:
            return _epoch_pairs_slow(rows)
        append_stamp(float(raw_stamp))
        append_text(str(raw_state).strip())
    return list(zip(stamps, texts, strict=True))


def _epoch_pairs_slow(rows: Sequence[Any]) -> list[tuple[float, Any]]:
    """Canonical parser for any recorder shape the fast path does not cover."""
    states, readings, _ = parse_history_payload({"_": list(rows)})
    # A numeric series lands in ``readings``; a state series in ``states``.
    pairs = list(readings.get("_") or states.get("_") or [])
    return [(stamp.timestamp(), value) for stamp, value in pairs]


def _state_series(rows: Sequence[Any]) -> list[tuple[float, bool]]:
    """``(epoch, on)`` pairs from state rows, dropping anything not on/off.

    ``unavailable``/``unknown`` are skipped, exactly as in correlation: an
    offline bridge says nothing about the load and treating it as "off" would
    invent a transition (ADR-0015).
    """
    out: list[tuple[float, bool]] = []
    for stamp, raw in _epoch_pairs(rows):
        text = str(raw).strip().lower()
        if text in ON_STATES:
            out.append((stamp, True))
        elif text in OFF_STATES:
            out.append((stamp, False))
    # Sorted once here, so both the transition instants and the resampling see
    # the same order.  A recorder payload is normally already chronological, but
    # relying on that would make a shuffled payload produce phantom transitions.
    out.sort(key=lambda item: item[0])
    return out


def _sorted_arrays(
    pairs: Sequence[tuple[float, Any]], *, numeric: bool
) -> tuple[np.ndarray, np.ndarray]:
    stamps = np.fromiter((stamp for stamp, _ in pairs), dtype=float, count=len(pairs))
    values = np.fromiter(
        (float(str(value)) for _, value in pairs) if numeric else
        (1.0 if value else 0.0 for _, value in pairs),
        dtype=float,
        count=len(pairs),
    )
    order = np.argsort(stamps, kind="stable")
    return stamps[order], values[order]


def _bin_starts(bins: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Start offset and count of each run of equal, sorted bin indices."""
    starts = np.flatnonzero(np.r_[True, bins[1:] != bins[:-1]])
    counts = np.diff(np.r_[starts, bins.size])
    return starts, counts


def _resample_row(
    pairs: Sequence[tuple[float, Any]], t0: float, n_bins: int
) -> np.ndarray:
    """Row watts on the grid: median per 15 s bin, held forward while fresh.

    Takes the pairs already parsed by :func:`_epoch_pairs`, for the same reason
    the state series does: each row is a 48 h series and parsing it twice (once
    for the window bounds, once for the fit) doubled the parse cost.

    The median per bin rejects a one-sample inrush spike without needing an
    outlier rule.  Zero-order hold fills the bins that have no sample at all,
    which is what makes a change-only channel usable: many Refoss CTs publish
    only when the value moves, so most bins are empty.  Holding the last reported
    value for up to :data:`MAX_GAP_S` is not interpolation -- it is the value the
    meter last reported -- and beyond that gap the bin is left unknown.
    """
    series = np.full(n_bins, np.nan)
    if not pairs:
        return series
    stamps, values = _sorted_arrays(pairs, numeric=True)
    bins = np.floor((stamps - t0) / BIN_S).astype(np.int64)
    inside = (bins >= 0) & (bins < n_bins)
    if not inside.any():
        return series
    bins, values, stamps = bins[inside], values[inside], stamps[inside]

    order = np.lexsort((values, bins))
    sb, sv, st = bins[order], values[order], stamps[order]
    starts, counts = _bin_starts(sb)
    unique_bins = sb[starts]
    # Exact median of each bin: the two middle order statistics.
    series[unique_bins] = (
        sv[starts + (counts - 1) // 2] + sv[starts + counts // 2]
    ) / 2.0

    last_sample = np.full(n_bins, -np.inf)
    last_sample[unique_bins] = np.maximum.reduceat(st, starts)
    have = np.isfinite(last_sample)
    index = np.maximum.accumulate(np.where(have, np.arange(n_bins), -1))
    valid = index >= 0
    held = np.where(valid, series[np.clip(index, 0, None)], np.nan)
    held_at = np.where(valid, last_sample[np.clip(index, 0, None)], -np.inf)
    grid_s = np.arange(n_bins) * BIN_S
    return np.where(valid & (grid_s - held_at <= MAX_GAP_S), held, np.nan)


def _resample_state(
    series: Sequence[tuple[float, bool]], t0: float, n_bins: int
) -> np.ndarray:
    """On/off on the grid, one value per bin, ``NaN`` before the first report.

    Takes the series already parsed by :func:`_state_series` rather than raw
    recorder rows: a light's history is parsed once for clustering and again for
    the design matrix otherwise, and a second parse is a second chance to
    disagree with the first about the same payload.

    There is deliberately **no** staleness horizon here, unlike a CT row.  A CT
    that stopped reporting is not measuring; a light that has not published for
    six hours is still in the state it last announced, because that is what a
    state entity means.  Applying the row's freshness rule would discard every
    long-lived "on" period and break the regressor for exactly the fixtures the
    fit needs.
    """
    out = np.full(n_bins, np.nan)
    if not series:
        return out
    stamps, values = _sorted_arrays(series, numeric=False)
    bins = np.floor((stamps - t0) / BIN_S).astype(np.int64)
    inside = (bins >= 0) & (bins < n_bins)
    if not inside.any():
        return out
    bins, values = bins[inside], values[inside]
    # The state that applies to a bin is the one announced by its last event:
    # an event at 12:00:07 belongs to the 12:00:00 bin and changes it from there.
    starts, counts = _bin_starts(bins)
    out[bins[starts]] = values[starts + counts - 1]
    return _forward_fill(out)


def _forward_fill(values: np.ndarray) -> np.ndarray:
    """Carry the last known value forward across ``NaN`` bins."""
    known = ~np.isnan(values)
    if not known.any():
        return values
    index = np.maximum.accumulate(np.where(known, np.arange(values.size), -1))
    return np.where(index >= 0, values[np.clip(index, 0, None)], np.nan)


def _instants(series: Sequence[tuple[float, bool]]) -> np.ndarray:
    """Every on→off or off→on instant of one entity, in epoch seconds."""
    out: list[float] = []
    previous: bool | None = None
    for stamp, on in series:
        if previous is not None and on != previous:
            out.append(stamp)
        previous = on
    return np.array(out, dtype=float)


def _co_toggle_shares(
    instants: Mapping[str, np.ndarray],
) -> dict[tuple[str, str], float]:
    """Share of each fixture's transition instants that coincide with another's.

    ``shares[(a, b)]`` is the share of ``a``'s transitions with a transition of
    ``b`` within :data:`CO_TOGGLE_S`; the relation is not symmetric, because a
    scene can fire a quiet fixture every time while the quiet fixture fires only
    occasionally on its own.

    Computed by projecting every entity's instants into one sorted array and
    reading the ``±CO_TOGGLE_S`` window around each entity at once.  The naive
    pairwise form is 40 000 ``searchsorted`` calls for 200 fixtures, which was
    three times the cost of the fits themselves; the projection makes it one
    window set per entity.  Distinct ``(source, target)`` pairs are counted, not
    raw events, so several of the target's transitions inside one window cannot
    push the share above 1.
    """
    ids = sorted(instants)
    if len(ids) < 2:
        return {}
    index = {entity_id: position for position, entity_id in enumerate(ids)}
    times = np.concatenate([instants[entity_id] for entity_id in ids])
    owners = np.concatenate(
        [np.full(instants[entity_id].size, index[entity_id], dtype=np.int64) for entity_id in ids]
    )
    order = np.argsort(times, kind="stable")
    times, owners = times[order], owners[order]

    shares: dict[tuple[str, str], float] = {}
    width = len(ids)
    for entity_id in ids:
        source = instants[entity_id]
        if source.size == 0:
            continue
        left = np.searchsorted(times, source - CO_TOGGLE_S, side="left")
        right = np.searchsorted(times, source + CO_TOGGLE_S, side="right")
        counts = right - left
        total = int(counts.sum())
        if total == 0:
            continue
        # Every (source instant, target event) inside the windows, deduplicated
        # to distinct source/target pairs.
        offsets = np.repeat(left - np.cumsum(counts) + counts, counts) + np.arange(total)
        targets = owners[offsets]
        sources = np.repeat(np.arange(source.size), counts)
        unique = np.unique(sources * width + targets)
        matched = np.bincount(unique % width, minlength=width)
        for target_position, count in enumerate(matched):
            if target_position == index[entity_id] or count == 0:
                continue
            shares[(entity_id, ids[target_position])] = count / source.size
    return shares


def _co_toggle_groups(instants: Mapping[str, np.ndarray]) -> list[list[str]]:
    """Union fixtures that fire within :data:`CO_TOGGLE_S` of each other.

    Same rule as the correlation module's withholding guard, used the other way
    round: there it decides two fixtures cannot be separated, here it decides
    they should be fitted as one regressor.  Sharing an *instant* is the
    discriminator, not sharing a circuit -- two fixtures on one row that switch
    independently stay separate groups and keep separate coefficients.
    """
    ids = sorted(instants)
    shares = _co_toggle_shares(instants)
    parent = {entity_id: entity_id for entity_id in ids}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for (first, second), forward in shares.items():
        if first > second:
            continue
        share = max(forward, shares.get((second, first), 0.0))
        if share < CO_TOGGLE_SHARE:
            continue
        root, other = find(first), find(second)
        if root != other:
            parent[other] = root

    grouped: dict[str, list[str]] = {}
    for entity_id in ids:
        grouped.setdefault(find(entity_id), []).append(entity_id)
    return sorted((sorted(members) for members in grouped.values()), key=lambda g: g[0])


def _design_matrix(
    series: Mapping[str, np.ndarray], groups: Sequence[Sequence[str]]
) -> np.ndarray:
    """One column per group: the fraction of the group that is on in that bin.

    A group column is unknown (``NaN``) when any member's state is unknown: the
    group's draw is the sum of its members, so not knowing one member means not
    knowing the group.  Averaging only the known members would silently shrink
    the column and bias the coefficient downward.
    """
    columns = []
    for group in groups:
        stack = np.column_stack([series[entity_id] for entity_id in group])
        complete = ~np.isnan(stack).any(axis=1)
        column = np.full(stack.shape[0], np.nan)
        column[complete] = stack[complete].mean(axis=1)
        columns.append(column)
    return np.column_stack(columns) if columns else np.empty((0, 0))


def _fit_row(
    watts: np.ndarray,
    design: np.ndarray,
    *,
    seed: int,
) -> tuple[np.ndarray, float, float, float] | None:
    """Fit one row; return ``(coefficients, base, r², null_p95)`` or ``None``.

    The intercept is free and unconstrained, so both the row *and* the design
    are centred before the non-negative solve.  Fitting ``nnls(X, y - ȳ)``
    instead -- which leaves the columns at their raw duty cycles -- is a
    different, wrong problem: an unconstrained ``base`` can absorb part of a
    column's mean, so the solver is forced to explain ``y - ȳ`` with coefficients
    that cannot move the intercept, and it systematically shrinks them.  On
    reconstructed data that cost a 60 W fixture 43 % of its value.  With centred
    columns the recovered coefficient is the same one an ordinary least squares
    fit with an intercept would give.

    The real coefficient comes from ``scipy.optimize.nnls``: ``w ≥ 0`` is a
    physical constraint, not a regulariser.

    The null deliberately does **not** use NNLS.  For each shuffle it takes the
    *unconstrained* least-squares optimum, which is the best R² any linear model
    could achieve on that shuffled design.  That is an upper bound on the NNLS
    null, so ``r2 - r2_null_p95`` is a *lower* bound on the true margin: if a row
    clears this null it would also clear the weaker NNLS one.  The alternative
    (running NNLS 50 times per row) is both slower by two orders of magnitude and
    slightly optimistic, because a sub-optimal null solve reports a smaller null
    R² and so an easier gate.
    """
    usable = ~np.isnan(watts) & ~np.isnan(design).any(axis=1)
    if int(usable.sum()) < 2:
        return None
    y = watts[usable]
    x = design[usable]
    if not np.any(np.ptp(x, axis=0) > 0.0):
        return None

    column_means = x.mean(axis=0)
    base_mean = float(y.mean())
    centred_y = y - base_mean
    total_ss = float(centred_y @ centred_y)
    if total_ss <= 0.0:
        return None
    centred_x = x - column_means

    gram = centred_x.T @ centred_x
    eigenvalues, vectors = np.linalg.eigh(gram)
    keep = eigenvalues > max(float(eigenvalues.max()) * _EIGENVALUE_FLOOR, 0.0)
    eigenvalues, vectors = eigenvalues[keep], vectors[:, keep]
    if not eigenvalues.size:
        return None
    # ``factor`` is ``(Λ^½ Vᵀ)``: ``factorᵀ factor == V Λ Vᵀ == XᵀX``.  Solving
    # ``nnls(factor, Λ^-½ Vᵀ Xᵀy)`` is the same non-negative problem as
    # ``nnls(X, y)`` up to an orthogonal transform, but the matrix handed to the
    # solver is ``n × n`` instead of ``m × n``.  On a 48 h window (11 520 bins ×
    # 200 entities) that is 9 ms instead of 400 ms per row, which is the whole
    # difference between the 2 s budget passing and failing.
    root = np.sqrt(eigenvalues)
    factor = (vectors * root).T
    project = (vectors / root).T

    def unconstrained(cross: np.ndarray) -> np.ndarray:
        """Best possible least-squares coefficients in the identifiable space."""
        return vectors @ ((vectors.T @ cross) / eigenvalues[:, None])

    cross = centred_x.T @ centred_y
    coefficient = nnls(factor, project @ cross)[0]
    # wᵀ(XᵀX)w = Σ λ_i (v_iᵀw)².  The eigenbasis is needed because the truncated
    # Gram is diagonal in ``v``, not in the coefficient's own axes.
    along = vectors.T @ coefficient
    residual = float(total_ss - 2.0 * coefficient @ cross + eigenvalues @ (along * along))
    r2 = 1.0 - max(residual, 0.0) / total_ss
    # The intercept the fit actually used: the row mean minus whatever mean the
    # chosen coefficients account for.  Reported so a reader can see the always-on
    # load the groups were fitted on top of.
    base = base_mean - float(column_means @ coefficient)

    # The null rotates the *whole* on/off matrix by a large offset: every
    # fixture keeps its own duty cycle, its ON-duration distribution and the
    # joint scene structure, and only the alignment with the row is destroyed.
    #
    # A plain i.i.d. shuffle is not a valid null here.  It also destroys the
    # autocorrelation of the design (a light that is on for an hour becomes
    # scattered single bins), which makes the null fit nearly worthless: measured
    # on 48 h of production data, the i.i.d. p95 was 0.001 while the block
    # rotation's was 0.68-0.76.  Against a null that weak, a row that merely
    # shares the house's evening duty cycle clears it -- exactly the confound the
    # gate exists to catch, and the reason every sub-panel CT "passed".
    #
    # The rotation offset is drawn from a fixed seed, so the output is
    # reproducible run to run; a threshold applied to a random number is not a
    # threshold.
    usable_bins = centred_y.size
    if usable_bins > 2 * NULL_MIN_SHIFT_BINS:
        rng = np.random.default_rng(seed)
        shifts = rng.integers(
            NULL_MIN_SHIFT_BINS, usable_bins - NULL_MIN_SHIFT_BINS, N_NULL_PERMUTATIONS
        )
        # Rolling the design's rows is the same null as rolling the row's values
        # the other way, and the latter needs one matvec per draw instead of
        # copying an ``m × n`` matrix: ``Σᵢ X[(i−s) mod m]·y[i] = X.T·roll(y, −s)``.
        # At 48 h × 200 entities the copies alone were 900 MB of memory traffic.
        draws = np.empty((usable_bins, N_NULL_PERMUTATIONS))
        for draw, shift in enumerate(shifts):
            draws[:, draw] = np.roll(centred_y, -int(shift))
        null_cross = centred_x.T @ draws
    else:
        # A window too short to rotate by a day gets the i.i.d. permutation.  It
        # is the weaker null, so on a short window the margin gate is more
        # permissive -- and that is stated rather than pretended away, because
        # the alternative (refusing to fit short windows at all) would reject
        # every test fixture.
        rng = np.random.default_rng(seed)
        draws = np.empty((usable_bins, N_NULL_PERMUTATIONS))
        for draw in range(N_NULL_PERMUTATIONS):
            draws[:, draw] = centred_y[rng.permutation(usable_bins)]
        null_cross = centred_x.T @ draws

    null_coefficient = unconstrained(null_cross)
    null_along = vectors.T @ null_coefficient
    null_residual = (
        total_ss
        - 2.0 * np.einsum("ij,ij->j", null_coefficient, null_cross)
        + eigenvalues @ (null_along * null_along)
    )
    null_r2 = 1.0 - np.maximum(null_residual, 0.0) / total_ss
    return coefficient, base, r2, float(np.percentile(null_r2, 95))


def fit_shared_rows(
    *,
    row_readings: Mapping[str, list[dict]],
    entity_states: Mapping[str, list[dict]],
    entity_levels: Mapping[str, Mapping[datetime, float]],
    row_noise_w: Mapping[str, float],
    min_transitions_for_fit: int = 20,
    min_r2_improvement: float = 0.30,
    min_r2_margin_over_null: float = 0.15,
    seed: int = 0,
    excluded_ct_ids: frozenset[str] = frozenset(),
) -> tuple[GroupFit, ...]:
    """Fit every eligible circuit row as a sum of co-toggling groups.

    ``row_readings`` are the CT series and ``entity_states`` the on/off history
    of the candidate fixtures, both as recorder dicts
    (``{"s": ..., "lu": ...}``); already-parsed ``(datetime, value)`` pairs are
    accepted at runtime for callers that have normalised history themselves.
    ``entity_levels`` is any dimmer history the recorder carried and
    ``row_noise_w`` each row's quiet jitter (see
    :func:`wattmosaic_engine.ingestion.correlation.estimate_row_noise`).

    ``excluded_ct_ids`` is the row-type rule.  This function has no topology, so
    it cannot tell a branch circuit from an installation-wide feed or from a row
    that is already one device's dedicated measurement.  Emitting a "group" for
    either would be wrong in a way no consumer could detect -- a main feed's
    coefficient is the whole house, and a dedicated row's coefficient duplicates
    a device that already exists.  The caller owns that knowledge and passes the
    ids in; the default empty set means "no row is excluded", never "every row is
    acceptable".

    Rows are refused unless the fit beats a constant baseline by
    ``min_r2_improvement`` **and** beats its shuffled null by
    ``min_r2_margin_over_null``; both defaults are the measured production
    separation (see the module docstring).  ``min_transitions_for_fit`` guards
    the regressors themselves: a window in which the candidate fixtures changed
    state fewer than this many times has not observed them enough for a
    coefficient to mean anything, however good the in-sample R² looks.

    ``entity_levels`` is accepted because the caller has it and a dimmer
    regressor would need it, but it is **not** used as a multiplier: recorder
    history in this installation carries no brightness (0 of 81 lights), so the
    fit is binary on/off at whatever output the fixture was at.  The map is read
    only to add an evidence line when levels exist, so the function never
    silently claims a full-on wattage it did not measure.
    """
    if not row_readings or not entity_states:
        return ()

    parsed_states = {
        entity_id: _state_series(rows) for entity_id, rows in entity_states.items()
    }
    parsed_rows = {ct_id: _epoch_pairs(rows) for ct_id, rows in row_readings.items()}
    instants = {
        entity_id: _instants(series)
        for entity_id, series in parsed_states.items()
    }
    # Only fixtures that actually moved can carry a coefficient, and only the
    # first ``MAX_ENTITIES`` of them (in id order, so the cap is reproducible).
    active = sorted(entity_id for entity_id, events in instants.items() if events.size)
    if not active:
        return ()
    active = active[:MAX_ENTITIES]

    bounds = _axis_bounds(
        [parsed_states[entity_id] for entity_id in active]
        + [parsed_rows[ct_id] for ct_id in row_readings]
    )
    if bounds is None:
        return ()
    t0, t1 = bounds
    n_bins = int(math.floor((t1 - t0) / BIN_S)) + 1
    if n_bins < 2:
        return ()

    groups = _co_toggle_groups({entity_id: instants[entity_id] for entity_id in active})
    series = {
        entity_id: _resample_state(parsed_states[entity_id], t0, n_bins)
        for entity_id in active
    }
    design = _design_matrix(series, groups)
    if design.size == 0:
        return ()

    # How many times did the regressors actually move in the window?  An
    # aggregate fit on a nearly static design has nothing to attribute, and the
    # in-sample R² of such a fit is an artefact of one or two bins.
    delta = np.diff(design, axis=0)
    moved = (delta != 0.0) & ~np.isnan(delta)
    if int(moved.sum()) < min_transitions_for_fit:
        return ()

    levels_present = any(bool(levels) for levels in entity_levels.values())
    fits: list[GroupFit] = []
    for ct_id in sorted(row_readings):
        if ct_id in excluded_ct_ids:
            continue
        watts = _resample_row(parsed_rows[ct_id], t0, n_bins)
        result = _fit_row(watts, design, seed=seed)
        if result is None:
            continue
        coefficient, base, r2, null_p95 = result
        margin = r2 - null_p95
        if r2 < min_r2_improvement or margin < min_r2_margin_over_null:
            continue
        floor_w = max(MIN_RESPONSE_W, float(row_noise_w.get(ct_id, 0.0)))
        for column, group in enumerate(groups):
            total_w = float(coefficient[column])
            if total_w < floor_w:
                # Below the engine's own response floor (or below the row's
                # jitter): the arithmetic is real, the measurement is not.
                continue
            identifiable = len(group) == 1
            fits.append(
                GroupFit(
                    ct_id=ct_id,
                    total_w=total_w,
                    r2=r2,
                    r2_null_p95=null_p95,
                    r2_margin=margin,
                    members=tuple(
                        GroupMember(
                            entity_id=entity_id,
                            estimated_w=total_w if identifiable else None,
                        )
                        for entity_id in group
                    ),
                    evidence=_evidence(
                        group=group,
                        total_w=total_w,
                        base=base,
                        r2=r2,
                        null_p95=null_p95,
                        margin=margin,
                        levels_present=levels_present,
                    ),
                )
            )

    fits.sort(key=lambda fit: (fit.ct_id, -fit.total_w, fit.members[0].entity_id))
    return tuple(fits)


def _axis_bounds(
    series: Iterable[Sequence[tuple[float, Any]]],
) -> tuple[float, float] | None:
    """Earliest and latest sample across every series, in epoch seconds."""
    lo: float | None = None
    hi: float | None = None
    for rows in series:
        if not rows:
            continue
        first = rows[0][0]
        last = rows[-1][0]
        low, high = (first, last) if first <= last else (last, first)
        lo = low if lo is None else min(lo, low)
        hi = high if hi is None else max(hi, high)
    if lo is None or hi is None:
        return None
    return lo, hi


def _evidence(
    *,
    group: Sequence[str],
    total_w: float,
    base: float,
    r2: float,
    null_p95: float,
    margin: float,
    levels_present: bool,
) -> tuple[str, ...]:
    """Human sentences for one group fit.  Shown, never parsed."""
    count = len(group)
    if count == 1:
        sharing = f"1 fixture on this circuit, no co-toggling partner ({group[0]})"
    else:
        sharing = f"{count} fixtures co-toggle within {CO_TOGGLE_S:g} s and share one switch"
    lines = [
        sharing,
        (
            f"measured together at {total_w:.1f} W on a {base:.1f} W baseline; "
            f"R² {r2:.2f}, {margin:+.2f} above a shuffled null ({null_p95:.2f})"
        ),
    ]
    if count > 1:
        lines.append(
            "individual shares are not separable from the data, so none is claimed"
        )
    if levels_present:
        lines.append(
            "no brightness history was recorded, so the coefficient is the draw at "
            "the output the fixtures were at"
        )
    return tuple(lines)


__all__ = [
    "BIN_S",
    "MAX_ENTITIES",
    "MAX_GAP_S",
    "N_NULL_PERMUTATIONS",
    "GroupFit",
    "GroupMember",
    "fit_shared_rows",
]
