"""Read helpers for archived (legacy-basis) model-version rows.

Contract 3.0 removed the old flat-contract model objects.  The archived
``device_model_versions`` table (renamed ``*_legacy`` by migration 25) is kept
read-only for one release, and a handful of read paths — the mapping projection,
the model-view adapter, diagnostics — still need to turn one of those rows into
a small, duck-typed value.  This module owns that conversion and nothing else:
the v3 codec for live, versioned specs lives in :mod:`models.spec`.

The returned :class:`LegacyModelRow` carries the legacy field names as *data*
(the columns and JSON keys are fixed by the archived schema) but is not itself
a legacy contract model: it is a local, read-only view with a ``model_dump`` that
returns the same JSON shape those read paths have always consumed.  No live
code path may treat it as validated power (DEC-31).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..contracts import OperatingStage

# ``SchemaVersionError`` moved into the v3 codec in :mod:`models.spec`; this
# re-export keeps the long-standing import path working for callers that still
# use it while making ``spec`` the single definition site.
from .spec import SchemaVersionError

__all__ = [
    "LegacyModelRow",
    "SchemaVersionError",
    "parameters_from_json",
    "parse_timestamp",
    "version_from_row",
    "versions_from_rows",
]


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


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


@dataclass(slots=True)
class LegacyParameters:
    """A read-only view of an archived model's flat parameter blob."""

    on_power_w: float | None = None
    off_power_w: float | None = None
    standby_power_w: float | None = None
    startup_power_w: float | None = None
    shutdown_power_w: float | None = None
    curve: list[tuple[float, float]] = field(default_factory=list)
    state_map: dict[str, float] = field(default_factory=dict)
    stages: list[Any] = field(default_factory=list)
    residual_mean_w: float | None = None
    residual_std_w: float | None = None

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return {
            "on_power_w": self.on_power_w,
            "off_power_w": self.off_power_w,
            "standby_power_w": self.standby_power_w,
            "startup_power_w": self.startup_power_w,
            "shutdown_power_w": self.shutdown_power_w,
            "curve": [list(p) for p in self.curve],
            "state_map": dict(self.state_map),
            "stages": [
                s.model_dump(mode="json") if hasattr(s, "model_dump") else dict(s)
                for s in self.stages
            ],
            "residual_mean_w": self.residual_mean_w,
            "residual_std_w": self.residual_std_w,
        }


@dataclass(slots=True)
class LegacyModelRow:
    """A read-only, duck-typed view of one archived model-version row."""

    device_id: str
    model_type: str
    version: int
    status: str
    created_at: datetime
    trained_from: datetime | None
    trained_to: datetime | None
    sample_count: int
    training_quality: float
    validation_metrics: dict[str, float]
    parameters: LegacyParameters
    confidence: float
    notes: list[str]

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "model_type": self.model_type,
            "version": self.version,
            "status": self.status,
            "created_at": self.created_at,
            "trained_from": self.trained_from,
            "trained_to": self.trained_to,
            "sample_count": self.sample_count,
            "training_quality": self.training_quality,
            "validation_metrics": dict(self.validation_metrics),
            "parameters": self.parameters.model_dump(mode=mode),
            "confidence": self.confidence,
            "notes": list(self.notes),
        }


def parameters_from_json(raw: Any) -> LegacyParameters | None:
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
        return LegacyParameters(
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


def version_from_row(row: dict[str, Any]) -> LegacyModelRow | None:
    """Build a read-only view of an archived ``device_model_versions`` row."""
    if not row:
        return None
    model_type = str(row.get("model_type") or "")
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
    status = str(row.get("status") or "pending")
    return LegacyModelRow(
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


def versions_from_rows(rows: list[dict[str, Any]]) -> list[LegacyModelRow]:
    out: list[LegacyModelRow] = []
    for row in rows:
        version = version_from_row(row)
        if version is not None:
            out.append(version)
    return out
