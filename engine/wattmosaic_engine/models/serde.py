"""Serialisation helpers for device-model versions.

The storage layer persists :class:`DeviceModelVersion` as flat columns with
JSON blobs.  This module is the single place that knows how to turn a DB row
back into a contract object, and how to build a new version number.  It is
deliberately tolerant: corrupt JSON yields ``None`` rather than crashing the
inference loop (spec §21: one malformed record must not stop the engine).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..contracts import (
    DeviceModelParameters,
    DeviceModelVersion,
    ModelStatus,
    ModelType,
    OperatingStage,
)


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def parameters_from_json(raw: Any) -> DeviceModelParameters | None:
    data = _json(raw, None)
    if not isinstance(data, dict):
        return None
    try:
        stages = []
        for stage in data.get("stages") or []:
            if isinstance(stage, dict):
                stages.append(OperatingStage(**stage))
        curve = data.get("curve") or []
        curve = [(float(a), float(b)) for a, b in curve if len((a, b)) == 2]
        state_map = {
            str(k): float(v)
            for k, v in (data.get("state_map") or {}).items()
            if isinstance(v, (int, float))
        }
        return DeviceModelParameters(
            on_power_w=_opt_float(data.get("on_power_w")),
            off_power_w=_opt_float(data.get("off_power_w")),
            standby_power_w=_opt_float(data.get("standby_power_w")),
            startup_power_w=_opt_float(data.get("startup_power_w")),
            shutdown_power_w=_opt_float(data.get("shutdown_power_w")),
            curve=curve,
            state_map=state_map,
            stages=stages,
            residual_mean_w=_opt_float(data.get("residual_mean_w")),
            residual_std_w=_opt_float(data.get("residual_std_w")),
        )
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def version_from_row(row: dict[str, Any]) -> DeviceModelVersion | None:
    """Build a contract ``DeviceModelVersion`` from a ``device_model_versions`` row."""
    if not row:
        return None
    try:
        model_type_raw = row.get("model_type")
        model_type = ModelType(str(model_type_raw))
    except ValueError:
        return None
    try:
        version = int(row.get("version"))
    except (TypeError, ValueError):
        return None
    parameters = parameters_from_json(row.get("parameters_json"))
    if parameters is None:
        return None
    metrics_raw = _json(row.get("validation_metrics_json"), {})
    metrics = {
        str(k): float(v)
        for k, v in (metrics_raw or {}).items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    notes = _json(row.get("notes_json"), [])
    if not isinstance(notes, list):
        notes = [str(notes)]
    status_raw = row.get("status") or ModelStatus.PENDING.value
    try:
        status = ModelStatus(str(status_raw))
    except ValueError:
        status = ModelStatus.PENDING
    return DeviceModelVersion(
        device_id=str(row.get("device_id") or ""),
        model_type=model_type,
        version=version,
        status=status,
        created_at=parse_timestamp(row.get("created_at")) or datetime.now(UTC),
        trained_from=parse_timestamp(row.get("trained_from")),
        trained_to=parse_timestamp(row.get("trained_to")),
        sample_count=int(row.get("sample_count") or 0),
        training_quality=float(row.get("training_quality") or 0.0),
        validation_metrics=metrics,
        parameters=parameters,
        confidence=float(row.get("confidence") or 0.0),
        notes=[str(n) for n in notes],
    )


def versions_from_rows(rows: list[dict[str, Any]]) -> list[DeviceModelVersion]:
    out: list[DeviceModelVersion] = []
    for row in rows:
        version = version_from_row(row)
        if version is not None:
            out.append(version)
    return out


def next_version(existing: list[DeviceModelVersion]) -> int:
    return max((v.version for v in existing), default=0) + 1


__all__ = [
    "next_version",
    "parameters_from_json",
    "parse_timestamp",
    "version_from_row",
    "versions_from_rows",
]
