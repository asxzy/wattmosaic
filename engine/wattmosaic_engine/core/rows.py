"""Row <-> contract rehydration.

Stored SQLite rows are plain dicts of ISO text and NULLs; these helpers turn them
back into contract models.  Owned by D3 after X03 (null semantics)."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..contracts import InferenceResult, Reconciliation, utcnow


def _json_list(raw: Any, default: list[Any] | None = None) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else (default or [])
        except ValueError:
            return default or []
    return default or []


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def row_to_inference_result(row: dict[str, Any]) -> InferenceResult:
    return InferenceResult(
        device_id=row["device_id"],
        timestamp=row["timestamp"],
        power_w=_safe_float(row.get("power_w")),
        energy_kwh=None if row.get("energy_kwh") is None else _safe_float(row["energy_kwh"]),
        source=row.get("source") or "unknown",
        confidence=_safe_float(row.get("confidence")),
        model_id=row.get("model_id"),
        model_version=row.get("model_version"),
        operating_stage=row.get("operating_stage"),
        stage_status=row.get("stage_status") or "unknown",
        sample_count=int(row.get("sample_count") or 0),
        quality_status=row.get("quality_status") or "good",
        measurement_age_seconds=_safe_float(row.get("measurement_age_seconds")),
        supporting_ct_channels=_json_list(row.get("supporting_ct_channels_json")),
        explanation=_json_list(row.get("explanation_json")),
        provenance=_json_obj(row.get("provenance_json")),
        confidence_tier=row.get("confidence_tier") or "tier_0_unknown",
        monitoring_level=row.get("monitoring_level") or "unknown",
        power_behavior=row.get("power_behavior") or "unknown",
        tier_reasons=_json_list(row.get("tier_reasons_json")),
    )


def row_to_reconciliation(
    row: dict[str, Any] | None,
    *,
    clock: Callable[[], datetime] = utcnow,
) -> Reconciliation:
    if not row:
        return Reconciliation()
    return Reconciliation(
        timestamp=row.get("timestamp") or clock(),
        topology_version=int(row.get("topology_version") or 1),
        main_power_w=_safe_float(row.get("main_power_w")),
        l1_power_w=None if row.get("l1_power_w") is None else _safe_float(row["l1_power_w"]),
        l2_power_w=None if row.get("l2_power_w") is None else _safe_float(row["l2_power_w"]),
        direct_measured_w=_safe_float(row.get("direct_measured_w")),
        inferred_w=_safe_float(row.get("inferred_w")),
        model_estimated_w=_safe_float(row.get("model_estimated_w")),
        configured_prior_w=_safe_float(row.get("configured_prior_w")),
        unknown_residual_w=_safe_float(row.get("unknown_residual_w")),
        #: Contract 1.4.0 residual split.  Rows written before schema 7 have no
        #: value (NULL) -- they genuinely never recorded an answer, so they read
        #: as the model default ``0.0`` rather than a fabricated split.
        unmonitored_w=_safe_float(row.get("unmonitored_w")),
        meter_disagreement_w=_safe_float(row.get("meter_disagreement_w")),
        #: Contract 1.5.0.  ``meter_spread_w`` is the unbounded meter-to-meter
        #: spread, distinct from the residual-clipped ``meter_disagreement_w``.
        #: A pre-schema-7 row has NULL and reads as the model default ``0.0``.
        meter_spread_w=_safe_float(row.get("meter_spread_w")),
        #: SQLite stores the flag as 0/1; NULL (an older row) is not a claim
        #: that the spread exceeded the residual.
        meter_spread_exceeds_residual=bool(row.get("meter_spread_exceeds_residual")),
        reconciliation_error_w=_safe_float(row.get("reconciliation_error_w")),
        reconciliation_error_ratio=_safe_float(row.get("reconciliation_error_ratio")),
        accounted_fraction=_safe_float(row.get("accounted_fraction")),
        measurement_uncertainty_w=_safe_float(row.get("measurement_uncertainty_w")),
        #: SQLite stores the flag as 0/1; NULL (a pre-schema-7 row) is not
        #: "confirmed" -- absent must never read as ``True``.
        topology_confirmed=bool(row.get("topology_confirmed")),
        quality_status=row.get("quality_status") or "good",
        explanation=_json_list(row.get("explanation_json")),
    )
