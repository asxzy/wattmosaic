"""Build the diagnostics payload exposed at ``GET /api/v1/diagnostics``.

The report must be safe to hand to a user or an issue tracker:

* No tokens, passwords, API keys, Authorization headers or credentials in URLs.
* Only the redacted configuration view (``EngineConfig.redacted()``) is used.
* Every value is JSON-serialisable: datetimes become ISO strings, enums become
  their values, models are dumped.

The function accepts *components*, not the whole engine, so it can be used from
``main``, from tests, or from a standalone diagnostics CLI.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from ..contracts import CONTRACT_VERSION, HealthResponse, utcnow
from ..deprecations import collect as collect_deprecations
from ..deprecations import declares_dedicated

log = logging.getLogger("wattmosaic.diagnostics")

REDACTED = "***"
_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "credential",
    "api_key",
    "apikey",
    "private_key",
    "access_key",
    "cookie",
)


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact secrets and normalise to JSON-safe primitives."""
    if key is not None and is_sensitive_key(key) and value not in (None, "", REDACTED):
        return REDACTED
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        dt = value if isinstance(value, datetime) else datetime(value.year, value.month, value.day)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return redact(value.model_dump(mode="json"))
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "value") and isinstance(value.value, (str, int, float)):
        return value.value
    return str(value)


def _safe_call(source: Any, name: str, default: Any = None) -> Any:
    if source is None:
        return default
    fn = getattr(source, name, None)
    if fn is None:
        return default
    try:
        return fn() if callable(fn) else fn
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail
        log.warning("diagnostics call %s.%s failed: %s", type(source).__name__, name, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _db_section(database: Any) -> dict[str, Any]:
    if database is None:
        return {"available": False, "reason": "not configured"}
    integrity = _safe_call(database, "integrity_check", {})
    stats = _safe_call(database, "stats", {})
    healthy = isinstance(integrity, dict) and integrity.get("integrity") == "ok"
    return {
        "available": True,
        "healthy": healthy,
        "integrity": integrity,
        "stats": stats,
        "schema_current": (
            isinstance(integrity, dict)
            and integrity.get("schema_version") == integrity.get("latest_schema_version")
        ),
    }


def _health_section(
    health: HealthResponse | Mapping[str, Any] | None, core: Any
) -> dict[str, Any]:
    if health is None and core is not None:
        health = _safe_call(core, "health")
    if health is None:
        return {"status": "unknown"}
    data = health.model_dump(mode="json") if hasattr(health, "model_dump") else dict(health)  # type: ignore[arg-type]
    data.pop("details", None)
    return data


def _deprecations_section(
    config: Any, database: Any, core: Any, health_details: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Deprecated declarations, one object per ``ct_id`` (ADR-0029).

    Additive: an empty list when nothing is declared, a declaration exists, or
    the notice is switched off.  The notices themselves are messages; this is
    read-only, so the function never logs and never writes.

    An engine core already answers this for ``HealthResponse.details``; reusing
    that answer avoids reading the topology and device configurations twice.
    """
    if health_details is not None and "deprecations" in health_details:
        reported = health_details["deprecations"]
        return [dict(item) for item in reported] if isinstance(reported, list) else []
    if config is not None and not bool(
        getattr(config, "deprecate_dedicated_declaration", True)
    ):
        return []
    topology = _safe_call(core, "topology")
    if not isinstance(topology, Mapping) and not hasattr(topology, "channels"):
        topology = None
    if topology is None or not declares_dedicated(topology):
        # No in-memory declaration: nothing the configuration could add, and the
        # probe path must not read the database to prove a negative.
        return []
    configurations = _safe_call(database, "load_device_configurations")
    if not hasattr(configurations, "devices"):
        configurations = None
    try:
        found = collect_deprecations(topology, configurations)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail
        log.warning("deprecation scan failed: %s", exc)
        return []
    return [notice.as_dict() for notice in found]


def _health_details(health: Any) -> Mapping[str, Any] | None:
    """``HealthResponse.details`` as a mapping, or ``None`` when unavailable."""
    details = getattr(health, "details", None)
    if details is None and isinstance(health, Mapping):
        details = health.get("details")
    return details if isinstance(details, Mapping) else None


def _channel_section(aligner: Any) -> dict[str, Any]:
    health = _safe_call(aligner, "channel_health", {})
    if not isinstance(health, Mapping):
        return {"error": str(health)}
    out: dict[str, Any] = {}
    for name, value in health.items():
        out[str(name)] = value.to_dict() if hasattr(value, "to_dict") else redact(value)
    return out


def _quality_section(quality: Any) -> dict[str, Any]:
    report = getattr(quality, "last_report", None) if quality is not None else None
    if report is None:
        report = _safe_call(quality, "quality")
    if report is None:
        return {"available": False}
    data = report.model_dump(mode="json") if hasattr(report, "model_dump") else report
    return redact({"available": True, "report": data})


def _model_section(core: Any, database: Any) -> dict[str, Any]:
    models = _safe_call(core, "models_summary")
    if isinstance(models, dict) and models.get("error") is None:
        return redact(models)
    if database is not None:
        # Fall back to per-device model state from the database if possible.
        devices = _safe_call(database, "list_devices")  # not implemented; returns None
        if isinstance(devices, list):
            return {"devices": [redact(d) for d in devices]}
    return {"available": False, "reason": "no model registry wired"}


def _errors_section(database: Any) -> dict[str, Any]:
    if database is None:
        return {"available": False}
    try:
        recent = _safe_call(database, "recent_system_events", [])
        quality = _safe_call(database, "recent_quality_events", [])
    except Exception:  # noqa: BLE001
        recent, quality = [], []
    return {
        "available": True,
        "recent_system_events": redact(recent if isinstance(recent, list) else []),
        "recent_quality_events": redact(quality if isinstance(quality, list) else []),
    }


def collect_diagnostics(
    *,
    config: Any = None,
    database: Any = None,
    hasource: Any = None,
    aligner: Any = None,
    quality: Any = None,
    core: Any = None,
    extra: Mapping[str, Any] | None = None,
    error_limit: int = 20,
) -> dict[str, Any]:
    """Assemble the diagnostics report as a plain JSON-safe dict."""
    generated = utcnow()
    core_health = _safe_call(core, "health") if core is not None else None
    health = _health_section(core_health, None)
    engine_health = {
        "status": health.get("status", "unknown"),
        "contract_version": health.get("contract_version", CONTRACT_VERSION),
        "engine_version": health.get("engine_version"),
        "uptime_seconds": health.get("uptime_seconds"),
        "learning_active": health.get("learning_active"),
        "queue_depth": health.get("queue_depth"),
        "inference_last_run_at": health.get("inference_last_run_at"),
        # Frozen additive key (ADR-0029).  Always present, possibly empty: a
        # consumer that looks for it must not have to distinguish "absent" from
        # "nothing deprecated".  The core's own answer is reused when present so
        # this never scans the topology twice.
        "deprecations": _deprecations_section(
            config, database, core, _health_details(core_health)
        ),
    }
    ha = {
        "connected": bool(_safe_call(hasource, "connected", False)) if hasource is not None else False,
        "state": _safe_call(hasource, "state"),
        "last_event_at": _safe_call(hasource, "last_event_at"),
        "last_error": _safe_call(hasource, "last_error"),
        "reconnect_attempt": _safe_call(hasource, "reconnect_attempt", 0),
        "stats": _safe_call(hasource, "stats", {}),
    }
    aligner_stats = _safe_call(aligner, "stats", {})
    last_success = _safe_call(core, "last_successful_processing_at")
    report = {
        "generated_at": generated,
        "contract_version": CONTRACT_VERSION,
        "engine_health": engine_health,
        "home_assistant": ha,
        "channels": _channel_section(aligner),
        "aligner": aligner_stats,
        "models": _model_section(core, database),
        "inference_quality": _quality_section(quality),
        "database": _db_section(database),
        "last_successful_processing_at": last_success,
        "errors": _errors_section(database),
        "configuration": redact(config.redacted()) if config is not None and hasattr(config, "redacted") else {},
        "runtime": _safe_call(core, "runtime_stats", {}),
    }
    if extra:
        report["extra"] = redact(dict(extra))
    # Guarantee JSON-safety and redaction even for values added by components.
    merged = redact(report)
    if isinstance(merged, dict):
        merged.setdefault(
            "database",
            {"available": False},
        )
        merged["error_limit"] = error_limit
        merged["ok"] = _overall_ok(merged)
    return merged


def _overall_ok(report: Mapping[str, Any]) -> bool:
    engine = report.get("engine_health") or {}
    if engine.get("status") == "error":
        return False
    database = report.get("database") or {}
    return not (database.get("available") and database.get("healthy") is False)


def run_diagnostics(**kwargs: Any) -> dict[str, Any]:
    """Alias kept for the public interface named in the task."""
    report = collect_diagnostics(**kwargs)
    # Round-trip through json to prove serialisability at the boundary.
    try:
        json.dumps(report)
    except (TypeError, ValueError):
        report = json.loads(json.dumps(report, default=str))
    return report


__all__ = ["collect_diagnostics", "is_sensitive_key", "redact", "run_diagnostics"]
