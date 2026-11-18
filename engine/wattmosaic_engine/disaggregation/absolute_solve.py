"""Measurement-system assembly and bounded NNLS solve (X02 split).

Moved verbatim from :mod:`~wattmosaic_engine.disaggregation.engine` as
module-level functions that take the engine configuration explicitly.  All
solver plumbing (slack columns, bounds assembly, row exclusion, relaxation and
declared-ceiling detection) lives here so I3 can reuse these pieces later.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from wattmosaic_engine.contracts import QualityStatus
from wattmosaic_engine.disaggregation._shared import (
    _PINNED_ZERO_W,
    _is_off,
    _is_solo_row,
    _state_power,
)
from wattmosaic_engine.disaggregation.configuration import EffectiveConfig
from wattmosaic_engine.electrical.matrix import MeasurementMatrix
from wattmosaic_engine.electrical.models import ModelView
from wattmosaic_engine.electrical.solver import (
    analyse_identifiability,
    augment_with_unknown_slack,
    solve_constrained_nnls,
)
from wattmosaic_engine.electrical.topology import TopologyGraph
from wattmosaic_engine.electrical.window import CoercedWindow


def _exclude_partial_legs(
    matrix: MeasurementMatrix,
    graph: TopologyGraph,
    window: CoercedWindow,
    mask: np.ndarray,
    excluded: dict[str, str],
    *,
    max_measurement_age_s: float,
    max_leg_skew_s: float,
    declared_leg_partner,
) -> None:
    """Drop rows that are one leg of a declared pair whose other leg is unusable.

    ``paired_ct_id`` declares that two CTs measure the two legs of *one*
    circuit.  A leg alone is not a measurement of that circuit at half its
    value -- it is a measurement of half of it at full scale, and no
    coefficient in the model knows that.  Using the live leg while its
    partner is stale silently halves the row target (the residual then
    absorbs the missing half and looks like unknown load), and using both
    when they are polling apart mixes two instants into one equation.

    So a pair is usable only as a pair: both legs fresh, both within
    ``max_leg_skew_s`` of each other.  Otherwise both rows are excluded and
    the reason says why, which is what lets reconciliation report the power
    as unknown instead of misattributing it.
    """
    if not mask.any():
        return
    rows = matrix.row_channel_ids
    seen: set[str] = set()
    for cid in rows:
        partner = declared_leg_partner(graph, cid)
        if partner is None:
            continue
        pair = cid if cid < partner else partner
        if pair in seen:
            continue
        seen.add(pair)
        # The partner must also be modelled as a row; if it is disabled or
        # has no coefficients there is nothing to compare against.
        i = matrix.row_index(cid)
        k = matrix.row_index(partner)
        if i is None or k is None:
            continue
        reasons: list[str] = []
        for leg in (cid, partner):
            reading = window.readings.get(leg)
            if reading is None or not reading.is_usable(max_measurement_age_s):
                reasons.append(f"partial_leg_measurement:{leg}")
        if not reasons:
            a = window.readings[cid].age_s
            b = window.readings[partner].age_s
            if math.isfinite(a) and math.isfinite(b) and abs(a - b) > max_leg_skew_s:
                reasons.append(
                    f"leg_skew_exceeded:{cid}:{partner}:"
                    f"{abs(a - b):.1f}s>{max_leg_skew_s:.1f}s"
                )
        if not reasons:
            continue
        reason = reasons[0]
        for leg, row in ((cid, i), (partner, k)):
            mask[row] = False
            excluded.setdefault(leg, reason)


def _declared_leg_partner(graph: TopologyGraph, ct_id: str) -> str | None:
    """The reciprocal ``paired_ct_id`` partner of ``ct_id``, if any.

    Reciprocity matters: a one-sided pointer is a discovery artefact, not a
    declaration that the two channels are legs of the same circuit.
    """
    ch = graph.channel(ct_id)
    partner_id = getattr(ch, "paired_ct_id", None) if ch is not None else None
    if not partner_id:
        return None
    partner = graph.channel(partner_id)
    if partner is None or getattr(partner, "paired_ct_id", None) != ct_id:
        return None
    return partner_id


def _solve_system(
    matrix: MeasurementMatrix,
    graph: TopologyGraph,
    window: CoercedWindow,
    device_ids: list[str],
    models: Mapping[str, ModelView | None],
    states: Mapping[str, str | None],
    diagnostics: dict[str, Any],
    state_overrides: Mapping[str, Mapping[str, str]] | None = None,
    configs: Mapping[str, EffectiveConfig] | None = None,
    *,
    max_measurement_age_s: float,
    max_leg_skew_s: float,
    model_ridge: float,
) -> dict[str, Any]:
    state_overrides = state_overrides or {}
    configs = configs or {}
    prepared = matrix.prepare(window.readings)
    n = len(device_ids)
    idx = {d: j for j, d in enumerate(device_ids)}
    mask = prepared.mask.copy()
    excluded = dict(prepared.excluded_rows)

    # Row usability: never use a stale/missing row.  Repeated rows are
    # down-weighted because they are not fresh physical samples.
    weights = np.array(matrix.row_weights, dtype=float)
    for i, cid in enumerate(matrix.row_channel_ids):
        reading = window.readings.get(cid)
        if reading is None:
            mask[i] = False
            excluded.setdefault(cid, "missing_measurement")
            continue
        if not reading.is_usable(max_measurement_age_s):
            mask[i] = False
            if reading.age_s > max_measurement_age_s:
                excluded.setdefault(cid, "stale_measurement")
            elif reading.quality in (QualityStatus.MISSING, QualityStatus.BAD):
                excluded.setdefault(cid, f"quality_{reading.quality}")
            else:
                excluded.setdefault(cid, "unusable_measurement")
            continue
        if reading.repeated:
            weights[i] *= 0.5

    _exclude_partial_legs(
        matrix,
        graph,
        window,
        mask,
        excluded,
        max_measurement_age_s=max_measurement_age_s,
        max_leg_skew_s=max_leg_skew_s,
        declared_leg_partner=_declared_leg_partner,
    )

    A = np.array(matrix.A, dtype=float)

    # Solve for every device that has at least one live coefficient.
    # Everything else is left at zero.
    solve_cols = [
        j for j in range(n) if np.any(np.abs(A[:, j]) > 0)
    ]
    if not solve_cols:
        solve_result = solve_constrained_nnls(
            np.zeros((0, 0)), np.zeros(0), device_ids=[], analyse=True
        )
        ident = analyse_identifiability(A, list(device_ids))
        return {
            "x": {d: 0.0 for d in device_ids},
            "index": idx,
            "identifiability": ident,
            "solve_result": solve_result,
        }

    A_sub = A[:, solve_cols]
    y_sub = prepared.y
    sub_ids = [device_ids[j] for j in solve_cols]

    # Main CT rows get an explicit non-negative "unknown load" slack.  The
    # main row then constrains the *sum* of known devices without forcing
    # the fit to invent device power for unmodelled household load.  Rows the
    # user declared shared get the same slack for the same reason (ADR-0011).
    shared_rows: list[str] = []
    for did, cfg in configs.items():
        if not cfg.shared_unmodelled:
            continue
        for cid in graph.attribution_ct_ids(did):
            if cid in matrix.row_channel_ids and cid not in shared_rows:
                shared_rows.append(cid)
    A_aug, slack_names, slack_rows = augment_with_unknown_slack(
        A_sub, matrix.row_channel_ids, matrix.row_kind, shared_rows=shared_rows
    )
    diagnostics["shared_rows_held_as_upper_bounds"] = shared_rows
    n_sub = len(sub_ids)
    row_has_support = np.array(
        [np.any(np.abs(A_aug[i]) > 0) for i in range(A_aug.shape[0])]
    )
    for i, cid in enumerate(matrix.row_channel_ids):
        if not row_has_support[i]:
            mask[i] = False
            if cid not in excluded:
                excluded[cid] = "row_without_device_support"

    upper: list[float | None] = []
    prior: list[float] = []
    state_pinned: list[str] = []
    for j in solve_cols:
        did = device_ids[j]
        view = models.get(did)
        model_power = _state_power(view, states.get(did))
        prior.append(0.0 if model_power is None else max(float(model_power), 0.0))
        # An explicitly OFF device is a *state constraint*: it cannot draw
        # more than its standby power.  Pinning it is what makes a shared
        # branch identifiable (the other device then owns the remainder).
        #
        # The cap is derived from evidence, never from the solver's freedom:
        #
        # * a model or configuration that states a standby draw allows that
        #   draw (plus the measurement tolerance) -- the device may genuinely
        #   consume it while "off";
        # * no stated standby means no evidence of *any* draw, so the cap is
        #   exactly zero.
        #
        # Without the second rule the row was only bounded and the solve was
        # under-determined: two devices on one branch CT returned an
        # arbitrary 190/10 split and credited 10 W to a lamp the user had
        # declared off.  Adding a tolerance floor "so standby can be
        # measured" fabricated attribution out of the solver's least-norm
        # tie-break, which is the one thing this project never does.
        #
        # A declared load signal is evidence in its own right (ADR-0012), so
        # the constraint must not require a learnt model.  Without this, a
        # shared circuit where every device is modelled only by its switch
        # stayed permanently unidentifiable -- the state told us which lamps
        # were dark and the engine ignored it.  The requirement is a
        # *declared* load entity: an undeclared state key proves nothing.
        cfg = configs.get(did)
        has_load_signal = bool(cfg is not None and cfg.load_entity_ids)
        if _is_off(states.get(did), state_overrides.get(did)) and (
            view is not None or has_load_signal
        ):
            standby = 0.0
            if view is not None:
                if view.params.standby_power_w is not None:
                    standby = abs(float(view.params.standby_power_w))
                elif view.params.off_power_w is not None:
                    standby = abs(float(view.params.off_power_w))
            if standby <= 0.0 and cfg is not None and cfg.expected_standby_w is not None:
                standby = abs(float(cfg.expected_standby_w))
            # No evidence of any draw means the cap really is zero.  The
            # bounded solver requires a strictly positive interval, so an
            # exact zero is expressed as the smallest representable cap and
            # the device is reported as pinned at zero.
            upper.append(
                2.0 * standby if standby > 0.0 else _PINNED_ZERO_W
            )
            state_pinned.append(did)
            continue
        # A model bound is a *prior*, never a cap on a measurement.  When
        # the device is alone in its attribution row the CT is a direct
        # observation of that device, so the measurement wins and no upper
        # bound is applied.  Bounds only help where the device shares a row
        # with others and would otherwise be free to absorb unknown load.
        if view is None or view.upper_bound_w is None or _is_solo_row(matrix, j):
            upper.append(None)
        else:
            upper.append(float(view.upper_bound_w))

    # A shared row carries loads nobody declared, and the solver has no way
    # to tell that surplus from a claimant's own draw: whichever column has
    # the widest bound absorbs it.  Measured on the reference installation,
    # ``lights_ct2`` peaked at 1 036 W while its three fixtures declared
    # 243.6 / 112.3 / 112.7 W; with one fixture ON and its siblings
    # positively OFF the whole row was credited to that one lamp -- 4.25x its
    # declared draw, graded ``tier_3_measured``.
    #
    # A *declared* wattage is the one bound that can prevent this.  Like a
    # state pin, it is user evidence rather than a learnt prior, so it is
    # never widened by the relaxation pass below.  On a solo row the CT is
    # still a direct observation of the device and the measurement wins.
    declared_bound: list[str] = []
    for k, did in enumerate(sub_ids):
        cfg = configs.get(did)
        if cfg is None or cfg.config_ceiling_w is None:
            continue
        j = solve_cols[k]
        if _is_solo_row(matrix, j):
            continue
        if did in state_pinned:
            continue
        # The declaration wins over a learnt model's bound: both are
        # evidence, but only the model's is explicitly revisable, and the
        # user's stated wattage is the stronger claim.
        upper[k] = float(cfg.config_ceiling_w)
        declared_bound.append(did)
    if declared_bound:
        diagnostics["declared_ceiling_applied"] = sorted(declared_bound)

    active_mask = mask & row_has_support
    if not np.any(active_mask):
        diagnostics["warnings"].append("no_usable_measurement_rows")
    active_idx = np.flatnonzero(active_mask)

    # A slack has no model, but it is still bounded by physics: because
    # every device power is >= 0, the unknown part of a main row cannot
    # exceed that row's measured value.  A finite bound also keeps the
    # bounded solver well conditioned (no ``inf`` in the active bounds).
    for row in slack_rows:
        row_value = float(y_sub[row]) if row < len(y_sub) else 0.0
        upper.append(max(0.0, row_value) + 1.0)
    prior.extend([0.0] * len(slack_names))
    all_ids = [*sub_ids, *slack_names]

    if active_idx.size:
        A_act = A_aug[active_idx] * weights[active_idx, None]
        y_act = y_sub[active_idx] * weights[active_idx]
        upper_arr = None
        if any(u is not None for u in upper):
            upper_arr = np.array(
                [np.inf if u is None else float(u) for u in upper], dtype=float
            )
        ridge = model_ridge
        if ridge > 0 and not any(p > 0 for p in prior[:n_sub]):
            ridge = 0.0
        solve_result = solve_constrained_nnls(
            A_act,
            y_act,
            device_ids=all_ids,
            weights=None,
            lower=0.0,
            upper=upper_arr,
            prior=prior if ridge > 0 else None,
            ridge=ridge,
            analyse=False,
        )
        # If a model bound made the fit materially worse than the CT rows
        # allow, relax the bound and refit.  The measurement is trusted over
        # the learnt model, and the relaxation is recorded explicitly.
        #
        # A *state* constraint is never relaxed.  "This lamp is off" is
        # evidence, not a model prior, and the refit exists because the
        # solver's least-norm tie-break otherwise invents a share for an
        # unobserved device.  Relaxing it would reintroduce exactly the
        # fabricated attribution the pin was added to prevent.
        #
        # A *declared* ceiling is treated the same way, for the same reason:
        # the user said what the fixture draws, so a shared row reading more
        # than that is evidence of undeclared load on the circuit, not
        # evidence that the declaration was wrong.
        if solve_result.at_upper_bound:
            relaxable = (
                set(solve_result.at_upper_bound)
                - set(state_pinned)
                - set(declared_bound)
            )
            relaxed = [
                None if did in relaxable else u
                for did, u in zip(all_ids, upper, strict=False)
            ]
            if any(u is not None for u in relaxed[:n_sub]):
                relaxed_arr = np.array(
                    [np.inf if u is None else float(u) for u in relaxed], dtype=float
                )
                relaxed_result = solve_constrained_nnls(
                    A_act,
                    y_act,
                    device_ids=all_ids,
                    weights=None,
                    lower=0.0,
                    upper=relaxed_arr,
                    prior=prior if ridge > 0 else None,
                    ridge=ridge,
                    analyse=False,
                )
                if relaxed_result.weighted_residual_norm < (
                    0.98 * solve_result.weighted_residual_norm
                ):
                    diagnostics["warnings"].append("model_upper_bound_relaxed")
                    # Report only what was actually relaxed.  A state-pinned
                    # device keeps its cap even when another device in the
                    # same solve gets a wider bound, so listing it here
                    # would claim evidence was overridden when it was not.
                    diagnostics.setdefault("relaxed_bounds", []).extend(
                        sorted(relaxable)
                    )
                    solve_result = relaxed_result
    else:
        solve_result = solve_constrained_nnls(
            np.zeros((0, len(all_ids))),
            np.zeros(0),
            device_ids=all_ids,
            lower=0.0,
            analyse=False,
        )

    # Identifiability is evaluated on the *device* columns of the same
    # augmented system: nuisance slacks must not be allowed to hide a
    # genuine device ambiguity, and device ambiguity must not be fabricated
    # by the presence of the slack.
    # Only rows that actually contribute to the fit may inform the
    # identifiability analysis.  A row that was excluded for staleness or a
    # missing nested child must not make a device look observable.
    # Identifiability is assessed on the *free* columns only.  A device
    # pinned by an explicit OFF state is fixed by an external constraint,
    # not estimated, so it cannot be part of a null-space ambiguity.
    pinned_set = set(state_pinned)
    free_local = [k for k, did in enumerate(sub_ids) if did not in pinned_set]
    ident_rows = np.flatnonzero(mask & row_has_support)
    ident = analyse_identifiability(
        A_aug[ident_rows][:, free_local + list(range(n_sub, len(all_ids)))],
        [sub_ids[k] for k in free_local] + slack_names,
        weights=weights[ident_rows],
        n_device_columns=len(free_local),
    )
    diagnostics["excluded_rows"].update(excluded)
    diagnostics["active_rows"] = [matrix.row_channel_ids[i] for i in active_idx]
    diagnostics["unknown_slack"] = {
        name.split(":", 1)[1]: float(solve_result.x[n_sub + k])
        for k, name in enumerate(slack_names)
        if k + n_sub < len(solve_result.x)
    }

    x = np.zeros(n)
    for k, j in enumerate(solve_cols):
        x[j] = float(solve_result.x[k]) if k < len(solve_result.x) else 0.0

    # A declared ceiling "binds" only when it is both active *and* the row
    # still carries power the solve could not place.  A lamp that genuinely
    # draws exactly its declared wattage also sits at its upper bound, and
    # that is a measurement, not a cap.  The difference is the residual: with
    # unexplained power on the row, the number the solver returned is the
    # declaration; with none, the row really did measure that much.
    declared_ceiling_hit: set[str] = set()
    if declared_bound and active_idx.size:
        x_dev = np.array(
            [float(solve_result.x[k]) for k in range(len(solve_result.x))],
            dtype=float,
        )
        predicted = A_aug @ x_dev
        for did in declared_bound:
            k = sub_ids.index(did)
            if did not in solve_result.at_upper_bound:
                continue
            for row_index in active_idx:
                if abs(float(A_sub[row_index, k])) <= 0.0:
                    continue
                leftover = float(y_sub[row_index] - predicted[row_index])
                tolerance = max(
                    3.0,
                    3.0 * float(matrix.row_sigma[row_index])
                    if row_index < len(matrix.row_sigma)
                    else 3.0,
                )
                if leftover > tolerance:
                    declared_ceiling_hit.add(did)
                    break
    if declared_ceiling_hit:
        diagnostics["declared_ceiling_hit"] = sorted(declared_ceiling_hit)

    return {
        "x": {d: float(x[idx[d]]) for d in device_ids},
        "index": idx,
        "identifiability": ident,
        "solve_result": solve_result,
        "active_rows": [matrix.row_channel_ids[i] for i in active_idx],
        "state_pinned": state_pinned,
        "at_upper_bound": set(solve_result.at_upper_bound),
        # Devices whose value is the *declared* wattage rather than an
        # observation: the row carried power beyond what the declaration
        # allows, so the surplus is undeclared load.  Callers must not grade
        # such a value as a measurement.
        "declared_ceiling_hit": declared_ceiling_hit,
    }
