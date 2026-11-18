"""Reconciliation and configured-prior bounding (X02 split).

Moved verbatim from :mod:`~wattmosaic_engine.disaggregation.engine` as
module-level functions that take engine state explicitly.  The residual is
never redistributed -- that contract is unchanged by the move.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from wattmosaic_engine.contracts import (
    InferenceResult,
    QualityStatus,
    Reconciliation,
    Source,
    TopologyStatus,
)
from wattmosaic_engine.electrical.main_power import main_power_w, meter_spread_w
from wattmosaic_engine.electrical.matrix import MeasurementMatrix
from wattmosaic_engine.electrical.topology import TopologyGraph
from wattmosaic_engine.electrical.window import CoercedWindow


def _bound_configured_priors_by_row(
    graph: TopologyGraph,
    window: CoercedWindow,
    results: Sequence[InferenceResult],
    diagnostics: dict[str, Any],
    *,
    max_measurement_age_s: float,
) -> None:
    """Bound configured priors by the power their own row actually carries.

    ADR-0025 bounds each claimant *individually*: a lamp declaring 150 W is
    never credited more than 150 W.  ADR-0026 bounds a *measured step* by the
    row's own reading.  Level 3b had neither, so a declaration was reported
    whenever its device's state said "on", whether or not the circuit carried
    that much.  Three fixtures declaring 60 + 120 + 240 W therefore reported
    420 W on a 58 W circuit, and because a ``configured_prior`` is subtracted
    from the whole-home residual exactly like a measurement, the surplus
    silently reduced the unknown load -- driving the residual negative when
    the declarations were large enough.

    A prior is the weakest evidence the engine has.  It may explain part of a
    row; the part that does not fit is undeclared load and stays in the
    residual.  Only ``CONFIGURED_PRIOR`` results are touched: a direct
    reading is an observation, a fit was already constrained by the solver,
    and a model estimate is a separate evidence class.

    The row is shared out **in proportion to what each claimant declared**, so
    no single declaration silently absorbs another device's share.  A row with
    no usable reading, or a stale one, is left alone: there is no present
    capacity to compare against, and substituting an out-of-date number would
    trade one wrong value for another.

    Devices are grouped by their *own* attribution row set rather than per CT,
    because a double-pole device legitimately sits on two legs -- treating the
    legs independently would clip it once per leg.
    """
    priors = [
        result
        for result in results
        if result.source == Source.CONFIGURED_PRIOR and result.power_w > 0.0
    ]
    if not priors:
        return
    by_device = {result.device_id: result for result in results}

    # Power already attributed to a row by any source other than a prior.
    # A prior may only spend what is left: the fit and any measurement have
    # first claim on the row they observed.
    claimed: dict[str, float] = {}
    for result in results:
        if result.source == Source.CONFIGURED_PRIOR or result.power_w <= 0.0:
            continue
        for cid in graph.attribution_ct_ids(result.device_id):
            claimed[cid] = claimed.get(cid, 0.0) + float(result.power_w)

    # Group by the tuple of rows the device attributes through.
    groups: dict[tuple[str, ...], list[str]] = {}
    for result in priors:
        rows = tuple(graph.attribution_ct_ids(result.device_id))
        if rows:
            groups.setdefault(rows, []).append(result.device_id)
    if not groups:
        return

    bounded: dict[str, float] = {}
    for rows, device_ids in groups.items():
        capacity = 0.0
        usable = True
        for cid in rows:
            reading = window.readings.get(cid)
            if reading is None:
                usable = False
                break
            # A stale row says nothing about what the circuit carries *now*.
            if reading.age_s > max_measurement_age_s:
                usable = False
                break
            value = reading.signed_w()
            # Reverse flow on a device row is not capacity to spend on a
            # declaration; leave the prior untouched rather than clip to 0.
            if value is None or value < 0.0:
                usable = False
                break
            capacity += float(value)
        if not usable:
            continue
        # Spend only what the row has left after measurement and fits.
        capacity -= sum(claimed.get(cid, 0.0) for cid in rows)
        if capacity <= 0.0:
            capacity = 0.0

        declared = sum(by_device[d].power_w for d in device_ids)
        if declared <= capacity + 1e-9:
            continue
        share = capacity / declared
        for device_id in device_ids:
            result = by_device[device_id]
            clipped = result.power_w * share
            bounded[device_id] = clipped
            result.power_w = clipped
            result.explanation = list(
                dict.fromkeys([*result.explanation, "declared_prior_bounded_by_row"])
            )
            result.provenance = {
                **dict(result.provenance or {}),
                "declared_prior_w": declared,
                "row_capacity_w": capacity,
                "row_capacity_ct_ids": list(rows),
                "row_shared_between": list(device_ids),
            }

    if bounded:
        diagnostics["declared_priors_bounded_by_row"] = {
            device_id: round(value, 6) for device_id, value in sorted(bounded.items())
        }


def _reconcile(
    graph: TopologyGraph,
    window: CoercedWindow,
    results: Sequence[InferenceResult],
    matrix: MeasurementMatrix,
    diagnostics: dict[str, Any],
    *,
    max_measurement_age_s: float,
    phase_split_l1: float,
) -> Reconciliation:
    inferred_w = sum(r.power_w for r in results if r.source == Source.INFERRED)
    model_w = sum(r.power_w for r in results if r.source == Source.MODEL_ESTIMATED)
    prior_w = sum(r.power_w for r in results if r.source == Source.CONFIGURED_PRIOR)
    accounted = inferred_w + model_w + prior_w

    main: Any = main_power_w(
        graph, window.readings, phase_split_l1=phase_split_l1
    )
    diagnostics["main_power"] = main.as_dict()

    if main.errors or not main.usable:
        unknown_residual = 0.0
        diagnostics["warnings"].append("main_power_unavailable")
    else:
        unknown_residual = main.total_w - accounted
        # A residual large enough to matter, with no state transition in
        # this cycle, is an event the state layer never saw: a load cycling
        # on its own, a device with no state entity, or a manual switch.
        # The engine cannot attribute it and must not invent a claimant, so
        # it records the fact and leaves the power in the residual.  This is
        # a diagnostic, not an ambiguity: ambiguity requires two candidates.
        has_transition = any(
            event.get("direction") for event in diagnostics.get("events", ())
        )
        if (
            not has_transition
            and main.complete
            and unknown_residual > max(50.0, 0.05 * abs(main.total_w))
        ):
            diagnostics["unexplained_residual_without_transition"] = {
                "residual_w": unknown_residual,
                "main_power_w": main.total_w,
                "reason": (
                    "measured power is not explained by any device or state "
                    "transition; left unattributed rather than fabricated"
                ),
            }

    # The residual and the meter-to-meter spread are *different kinds of
    # number* and must not be forced into one sum.
    #
    # ``unknown_residual`` is a bounded accounting remainder: it is exactly
    # ``main - accounted`` for the one canonical meter, and
    # ``accounted + unknown_residual == main.total_w`` depends on that.
    #
    # ``meter_spread`` is ``|canonical total - alias total|``: an
    # independent property of two instruments clamped on the same
    # conductors, with no upper bound at all.  On the reference install it
    # is ~349 W while the whole residual is ~166 W, so the spread is larger
    # than the residual and can never be "inside" it.
    #
    # What *is* a part of the residual is the disagreement's *contribution*
    # to it: at most the whole residual, because a residual cannot contain
    # more of anything than itself.  Hence:
    #
    #     meter_disagreement = clip(meter_spread, 0, residual)
    #     unmonitored        = residual - meter_disagreement
    #
    # which is additive by construction.  When the spread exceeds the
    # residual the whole residual is attributable to the instruments, and
    # ``meter_spread_exceeds_residual`` says so loudly: the residual is then
    # *not* evidence of unmonitored load.  Nothing is re-estimated, and the
    # residual itself is never rewritten.
    meter_spread = 0.0
    meter_disagreement = 0.0
    meter_spread_exceeds_residual = False
    if not (main.errors or not main.usable):
        meter_spread, disagreeing_cts = meter_spread_w(
            graph,
            window.readings,
            canonical_total_w=main.total_w,
            max_age_s=max_measurement_age_s,
            phase_split_l1=phase_split_l1,
        )
        if meter_spread > 0.0:
            # A residual below zero is a sample-vintage artefact, not a
            # quantity that can contain any part of a spread, so the
            # contribution is clipped at zero for it.
            meter_disagreement = min(meter_spread, max(0.0, unknown_residual))
            meter_spread_exceeds_residual = meter_spread > unknown_residual
            diagnostics["meter_disagreement"] = {
                "spread_w": meter_spread,
                "disagreement_w": meter_disagreement,
                "channels": list(disagreeing_cts),
                "exceeds_residual": meter_spread_exceeds_residual,
                "reason": (
                    "two CTs on the same conductors report different totals; "
                    "only the part that fits inside the residual is charged to "
                    "it, and the spread itself is an unbounded instrument "
                    "diagnostic, not load"
                ),
            }
    # Additive by construction.  For a non-negative residual both parts are
    # non-negative; a negative residual keeps its sign (it is a
    # sample-vintage artefact and is reported as-is, never clamped away).
    unmonitored = unknown_residual - meter_disagreement

    # Measurement-fit error: how well all attributed device power explains
    # the CT rows.  This is a *fit* diagnostic, not the power balance.
    prepared = matrix.prepare(window.readings)
    # matrix columns are the topology device order; build the vector by id.
    by_id = {r.device_id: r.power_w for r in results}
    x_all = np.array([by_id.get(d, 0.0) for d in matrix.col_device_ids], dtype=float)
    predictions = matrix.predict(x_all)
    row_errors = np.abs(prepared.y - predictions)
    # A topology whose devices are only visible through the home total has no
    # measurement rows at all.  ``np.array([])`` is float64 while ``mask`` is
    # bool, and ``&`` between them raises instead of yielding an empty result,
    # so build the row-support mask with an explicit dtype (defect #22).
    row_support = np.array(
        [np.any(np.abs(matrix.A[i]) > 0) for i in range(matrix.shape[0])],
        dtype=bool,
    )
    usable_rows = prepared.mask & row_support
    recon_error = float(np.sum(row_errors[usable_rows])) if np.any(usable_rows) else 0.0
    uncertainty = float(
        np.sqrt(np.sum(matrix.row_sigma[usable_rows] ** 2))
    ) if np.any(usable_rows) else 0.0

    denom = abs(main.total_w) if abs(main.total_w) > 1e-9 else None
    ratio = recon_error / denom if denom else 0.0
    if main.complete and main.total_w > 0:
        accounted_fraction = min(max(accounted / main.total_w, 0.0), 1.0)
    else:
        accounted_fraction = 0.0

    quality = QualityStatus.GOOD
    if (
        diagnostics["errors"]
        or diagnostics["ambiguity"]["ambiguous"]
        or diagnostics.get("excluded_rows")
        or main.missing_channels
        or main.errors
    ):
        quality = QualityStatus.DEGRADED
    if not main.complete:
        quality = QualityStatus.MISSING if not main.usable else QualityStatus.DEGRADED

    explanation = [
        "main_power_sum_of_parallel_mains",
        "residual_never_redistributed",
    ]
    if meter_disagreement > 0.0:
        explanation.append(
            f"residual_includes_meter_disagreement:{meter_disagreement:.1f}W"
            f"_of_{unknown_residual:.1f}W"
        )
    if unmonitored > 0.0 and meter_disagreement > 0.0:
        explanation.append(
            f"unmonitored_residual_excludes_meter_disagreement:{unmonitored:.1f}W"
        )
    if meter_spread_exceeds_residual:
        # Never hidden: when the two meters disagree by more than the whole
        # residual, the residual is *not* evidence of unmonitored load.  An
        # operator reading only ``unmonitored_w == 0`` could mistake that
        # for "everything is metered", which is the opposite of the fact
        # recorded here.
        explanation.append(
            f"meter_spread_exceeds_residual:{meter_spread:.1f}W"
            f"_vs_{unknown_residual:.1f}W"
        )
    if main.has_export:
        explanation.append("negative_power_export_preserved")
    if main.missing_channels:
        explanation.append("main_measurement_incomplete")
    if diagnostics["ambiguity"]["ambiguous"]:
        explanation.append("ambiguous_concurrent_events")
    if diagnostics["errors"]:
        explanation.append("per_device_error_isolated")
    # A negative residual means accounted power exceeds the measured main.
    # That is physically impossible for a snapshot taken at one instant, so
    # it is always a *sample-vintage* artefact: the mains and the branch CTs
    # were sampled at different times.  It is never evidence of double
    # counting (nested children are subtracted, mains are summed once), and
    # the negative value is reported as-is rather than clamped to zero,
    # because hiding it would hide a real data-quality problem.
    if unknown_residual < -max(1.0, 0.002 * abs(main.total_w)):
        explanation.append("negative_residual_sample_vintage_skew")
        diagnostics["warnings"].append("negative_residual_sample_vintage_skew")

    return Reconciliation(
        timestamp=window.at,
        topology_version=graph.topology.version,
        main_power_w=main.total_w,
        l1_power_w=main.l1_w,
        l2_power_w=main.l2_w,
        inferred_w=inferred_w,
        model_estimated_w=model_w,
        configured_prior_w=prior_w,
        unknown_residual_w=unknown_residual,
        unmonitored_w=unmonitored,
        meter_disagreement_w=meter_disagreement,
        meter_spread_w=meter_spread,
        meter_spread_exceeds_residual=meter_spread_exceeds_residual,
        reconciliation_error_w=recon_error,
        reconciliation_error_ratio=ratio,
        accounted_fraction=accounted_fraction,
        measurement_uncertainty_w=uncertainty,
        quality_status=quality,
        explanation=explanation,
        topology_confirmed=(
            str(getattr(graph.topology.status, "value", graph.topology.status))
            == TopologyStatus.CONFIRMED.value
        ),
    )
