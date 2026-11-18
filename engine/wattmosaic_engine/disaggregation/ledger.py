"""Joint-inference source ledger (X02 split).

Moved verbatim from :mod:`~wattmosaic_engine.disaggregation.engine` as a
module-level function that takes engine state explicitly.  The batch ledger is
computed once from the structures ``infer`` built and copied onto every result;
that behaviour is preserved exactly (I5 replaces it).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from wattmosaic_engine.contracts import InferenceResult, Source
from wattmosaic_engine.disaggregation._shared import SOURCE_IDS, _basis_value
from wattmosaic_engine.electrical.matrix import MeasurementMatrix
from wattmosaic_engine.electrical.models import ModelView
from wattmosaic_engine.electrical.topology import TopologyGraph
from wattmosaic_engine.electrical.window import CoercedWindow


def _source_ledger(
    *,
    graph: TopologyGraph,
    window: CoercedWindow,
    matrix: MeasurementMatrix,
    models: Mapping[str, ModelView | None],
    results: Sequence[InferenceResult],
    diagnostics: Mapping[str, Any],
    state_pinned: frozenset[str] | set[str] = frozenset(),
    residual_events: Sequence[Any] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Name every source used and every source available but unused, with a reason.

    This helper is pure: it reads the same structures ``infer`` already built and
    changes none of them, so the ledger can never alter a power estimate.  "Used"
    means this cycle's inference actually consumed the source; "unused" means the
    source was available (present in the inputs or observed in the window) but
    did not contribute, with a concrete code-derived reason read back off those
    structures.  Neither list is fabricated, and both are always returned so a
    consumer never confuses "no source named" with "field absent".
    """
    used: list[dict[str, str]] = []
    unused: list[dict[str, str]] = []

    # -- ha_state: a state transition or a changed previous-state snapshot. --
    transitions = diagnostics.get("events") or []
    changed = [e for e in transitions if e.get("direction")]
    if changed:
        used.append({"source": "ha_state", "reason": "state_transition_consumed"})
    elif transitions:
        unused.append(
            {"source": "ha_state", "reason": "transition_without_direction_change"}
        )
    else:
        unused.append({"source": "ha_state", "reason": "no_transition_in_window"})

    # -- ha_attributes: measured device levels published by the facade. -------
    levels = window.metadata.get("device_levels")
    if isinstance(levels, Mapping) and levels:
        reported = {str(k) for k in levels}
        consumed = [
            r.device_id
            for r in results
            if r.device_id in reported
            and r.provenance.get("configuration_source") == "measured_dimming_curve"
        ]
        if consumed:
            used.append(
                {
                    "source": "ha_attributes",
                    "reason": "device_level_read_for:" + ",".join(sorted(consumed)),
                }
            )
        else:
            unused.append(
                {
                    "source": "ha_attributes",
                    "reason": "level_reported_but_no_device_has_a_measured_curve",
                }
            )
    else:
        unused.append({"source": "ha_attributes", "reason": "no_device_level_attributes"})

    # -- smart_plug_measurement / ct_measurement: per-row instrument basis. ---
    excluded_rows = diagnostics.get("excluded_rows") or {}
    active = set(diagnostics.get("active_rows") or ())
    bases_present: dict[str, list[str]] = {}
    bases_active: dict[str, list[str]] = {}
    for cid in matrix.row_channel_ids:
        channel = graph.channel(cid)
        basis = _basis_value(getattr(channel, "measurement_basis", None))
        if basis == "monitor_reported":
            key = "smart_plug_measurement"
        elif basis == "ct_measured":
            key = "ct_measurement"
        else:
            continue
        bases_present.setdefault(key, []).append(cid)
        if cid in active:
            bases_active.setdefault(key, []).append(cid)
    for key, missing_reason in (
        ("smart_plug_measurement", "no_smart_plug_rows"),
        ("ct_measurement", "no_ct_measurement_rows"),
    ):
        present = bases_present.get(key, [])
        if not present:
            unused.append({"source": key, "reason": missing_reason})
            continue
        live = bases_active.get(key, [])
        if live:
            used.append(
                {"source": key, "reason": "rows_in_fit:" + ",".join(sorted(live))}
            )
        else:
            unused.append(
                {
                    "source": key,
                    "reason": "row_excluded:" + ",".join(
                        f"{cid}:{excluded_rows.get(cid, 'no_usable_measurement')}"
                        for cid in sorted(present)
                    ),
                }
            )

    # -- topology: the graph is always present and consumed by the solve. ----
    topology_reason = "graph_used_for_rows_and_identifiability"
    problems = graph.validate()
    if problems:
        topology_reason += ";validation_problems:" + ",".join(sorted(problems))
    used.append({"source": "topology", "reason": topology_reason})

    # -- residual_events: an optional caller-provided event stream, or the
    # unexplained-residual fact the reconcile step recorded.  ----------------
    if residual_events:
        used.append(
            {
                "source": "residual_events",
                "reason": "residual_events_consumed:" + str(len(list(residual_events))),
            }
        )
    elif diagnostics.get("unexplained_residual_without_transition"):
        used.append(
            {
                "source": "residual_events",
                "reason": "unexplained_residual_recorded_not_fabricated",
            }
        )
    elif changed:
        unused.append(
            {"source": "residual_events", "reason": "residual_explained_by_state_transition"}
        )
    else:
        unused.append({"source": "residual_events", "reason": "no_residual_events"})

    # -- learned_models: a resolved model that contributed a value. ----------
    resolved = {d: v for d, v in (models or {}).items() if v is not None}
    if not resolved:
        unused.append({"source": "learned_models", "reason": "no_model_for_device"})
    else:
        used_ids = sorted(
            {
                r.device_id
                for r in results
                if r.source == Source.MODEL_ESTIMATED and r.model_id
            }
        )
        if used_ids:
            used.append(
                {
                    "source": "learned_models",
                    "reason": "model_estimated_for:" + ",".join(used_ids),
                }
            )
        unused_ids = sorted(set(resolved) - set(used_ids))
        if unused_ids:
            reason = "model_resolved_but_not_used_for:" + ",".join(unused_ids)
            if state_pinned:
                reason += ";device_pinned_by_state:" + ",".join(
                    sorted(set(unused_ids) & set(state_pinned))
                )
            unused.append({"source": "learned_models", "reason": reason})

    order = {name: i for i, name in enumerate(SOURCE_IDS)}
    used.sort(key=lambda e: (order.get(e["source"], 99), e["source"]))
    unused.sort(key=lambda e: (order.get(e["source"], 99), e["source"]))
    return used, unused
