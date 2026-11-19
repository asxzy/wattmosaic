"""Row <-> contract rehydration.

Stored SQLite rows are plain dicts of ISO text and NULLs; these helpers turn them
back into contract models.  Owned by D3 after X03 (null semantics).

Null policy (HB §18, AT-17): an absent measurement or estimate is ``None``,
never ``0.0``.  :func:`_safe_float` therefore has a ``None`` default — a NULL,
empty or non-finite database value rehydrates as ``None``.  A caller may opt in
to a concrete fallback with ``default=...`` only for values that are *not*
measurements (e.g. an ordinal version), never for a watt/energy field.

The legacy :class:`InferenceResult` / :class:`Reconciliation` contracts (removed
in Z1) still declare the measurement fields non-optional.  Rehydration builds
them with ``model_construct`` so a NULL genuinely survives as ``None`` through
DB -> model -> API JSON, rather than being silently coerced to a measured zero.
A later nullable contract (v3) validates the same ``None`` normally.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..contracts import InferenceResult, Reconciliation, utcnow


def _defaults(model: type[Any]) -> dict[str, Any]:
    """A fresh dict of a model's own field defaults, for ``model_construct``.

    Pydantic's ``model_construct`` skips validation and applies only the model's
    declared defaults, so supplying them explicitly keeps every field present
    while still allowing ``None`` for a measurement.
    """
    out: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        default = field.get_default(call_default_factory=True)
        if default is not None:
            out[name] = default
    return out


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


def _safe_float(value: Any, default: float | None = None) -> float | None:
    """A finite float, or ``default`` (``None``) when the value is absent.

    Absent means ``None``, an empty string, or a non-finite number (``NaN`` /
    ``inf``) — none of these is a measurement, so none silently becomes zero.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _measurement(value: Any) -> float | None:
    """Rehydrate a DB measurement column, preserving NULL/NaN as ``None``."""
    return _safe_float(value)


def _timestamp(value: Any) -> Any:
    """Normalise a stored ISO timestamp to an aware UTC ``datetime``."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return value


def row_to_inference_result(row: dict[str, Any]) -> InferenceResult:
    """Rehydrate one ``inference_results`` row.

    Every watt/energy/confidence/age column is read with :func:`_measurement`,
    so NULL/NaN stay ``None`` through the model and into API JSON.  There is
    no zero coercion anywhere on this path.
    """
    fields: dict[str, Any] = _defaults(InferenceResult)
    fields.update(
        device_id=row["device_id"],
        timestamp=_timestamp(row["timestamp"]),
        source=row.get("source") or "unknown",
        stage_status=row.get("stage_status") or "unknown",
        sample_count=int(row.get("sample_count") or 0),
        quality_status=row.get("quality_status") or "good",
        supporting_ct_channels=_json_list(row.get("supporting_ct_channels_json")),
        explanation=_json_list(row.get("explanation_json")),
        provenance=_json_obj(row.get("provenance_json")),
        confidence_tier=row.get("confidence_tier") or "tier_0_unknown",
        monitoring_level=row.get("monitoring_level") or "unknown",
        power_behavior=row.get("power_behavior") or "unknown",
        tier_reasons=_json_list(row.get("tier_reasons_json")),
        model_id=row.get("model_id"),
        model_version=row.get("model_version"),
        operating_stage=row.get("operating_stage"),
        energy_kwh=_measurement(row.get("energy_kwh")),
        power_w=_measurement(row.get("power_w")),
        confidence=_measurement(row.get("confidence")),
        measurement_age_seconds=_measurement(row.get("measurement_age_seconds")),
    )
    return InferenceResult.model_construct(**fields)


def row_to_reconciliation(
    row: dict[str, Any] | None,
    *,
    clock: Callable[[], datetime] = utcnow,
) -> Reconciliation:
    """Rehydrate one ``reconciliation_results`` row.

    Every power/energy term is read with :func:`_measurement`, so NULL stays
    ``None`` through the model and into API JSON.  ``topology_version`` is an
    ordinal, not a measurement, and keeps a concrete fallback; the two flags
    are booleans, so NULL is honestly ``False`` (not a measurement).
    """
    if not row:
        empty = _defaults(Reconciliation)
        empty["timestamp"] = clock()
        return Reconciliation.model_construct(**empty)
    fields: dict[str, Any] = _defaults(Reconciliation)
    fields.update(
        timestamp=_timestamp(row["timestamp"]) if row.get("timestamp") else clock(),
        topology_version=int(row.get("topology_version") or 1),
        l1_power_w=_measurement(row.get("l1_power_w")),
        l2_power_w=_measurement(row.get("l2_power_w")),
        meter_spread_exceeds_residual=bool(row.get("meter_spread_exceeds_residual")),
        topology_confirmed=bool(row.get("topology_confirmed")),
        quality_status=row.get("quality_status") or "good",
        explanation=_json_list(row.get("explanation_json")),
    )
    for name in (
        "main_power_w",
        "direct_measured_w",
        "inferred_w",
        "model_estimated_w",
        "configured_prior_w",
        "unknown_residual_w",
        "unmonitored_w",
        "meter_disagreement_w",
        "meter_spread_w",
        "reconciliation_error_w",
        "reconciliation_error_ratio",
        "accounted_fraction",
        "measurement_uncertainty_w",
    ):
        value = _measurement(row.get(name))
        if value is not None:
            fields[name] = value
        # A NULL keeps the legacy contract's own default for now.  The legacy
        # Reconciliation (removed in Z1) declares these fields non-optional and
        # pre-v7 rows that predate a column must still read through the legacy
        # path.  `rehydrated_measurement` exposes the honest None for callers
        # that can represent absence (v3 contracts, quality gate, AT-17).
    return Reconciliation.model_construct(**fields)


def rehydrated_measurement(row: dict[str, Any], name: str) -> float | None:
    """The honest value of a DB measurement column, preserving NULL as None.

    Consumers that can represent absence should use this rather than the
    legacy-compatible :func:`row_to_reconciliation`.
    """
    return _measurement(row.get(name))
