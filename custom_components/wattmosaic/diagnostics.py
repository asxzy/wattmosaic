"""Diagnostics support for WattMosaic.

Produces a redacted snapshot of integration + engine state.  The API token is
**never** included: it is replaced by a boolean ``api_token_configured``.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_CT_ASSIGNMENTS,
    CONF_SCAN_INTERVAL,
    CONF_STALE_AFTER,
    CONF_UNAVAILABLE_AFTER,
    DOMAIN,
    VERSION,
)
from .coordinator import WattMosaicCoordinator
from .flow_logic import normalise_channels
from .util import redact_config

_LOGGER = logging.getLogger(__name__)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for one config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator: WattMosaicCoordinator | None = data.get("coordinator")
    client = data.get("client")

    config = {**entry.data, **entry.options}
    safe_config = redact_config(config)
    safe_config[CONF_API_TOKEN] = "**REDACTED**" if config.get(CONF_API_TOKEN) else ""

    state = getattr(entry, "state", None)
    diagnostics: dict[str, Any] = {
        "integration": {
            "domain": DOMAIN,
            "version": VERSION,
            "entry_id": entry.entry_id,
            "title": entry.title,
            "state": getattr(state, "value", state),
            "base_url": config.get(CONF_BASE_URL),
            "api_token_configured": bool(config.get(CONF_API_TOKEN)),
            "scan_interval_s": config.get(CONF_SCAN_INTERVAL),
            "stale_after_s": config.get(CONF_STALE_AFTER),
            "unavailable_after_s": config.get(CONF_UNAVAILABLE_AFTER),
        },
        "config": safe_config,
        "coordinator": {},
        "engine": {},
        "topology": {},
        "models": {},
        "last_errors": [],
    }

    if coordinator is None or client is None:
        diagnostics["last_errors"].append("coordinator not initialised")
        return diagnostics

    snapshot = coordinator.data.snapshot if coordinator.data else {}
    health = coordinator.data.health if coordinator.data else {}
    quality = (snapshot or {}).get("quality") or {}
    reconciliation = (snapshot or {}).get("reconciliation") or {}

    diagnostics["coordinator"] = {
        "engine_available": coordinator.engine_available,
        "last_success": coordinator.data.last_success.isoformat()
        if coordinator.data and coordinator.data.last_success
        else None,
        "received_at": coordinator.data.received_at.isoformat()
        if coordinator.data and coordinator.data.received_at
        else None,
        "snapshot_age_stale": coordinator.summary_stale(),
        "device_count": len(coordinator.data.results) if coordinator.data else 0,
        "last_error": coordinator.data.last_error if coordinator.data else None,
        # Audit trail of the topology replay: "pushed v5 at 12:03, engine now
        # v5" is answerable from a diagnostics download alone.
        **coordinator.topology_push_diagnostics,
    }
    if coordinator.data and coordinator.data.last_error:
        diagnostics["last_errors"].append(coordinator.data.last_error)

    diagnostics["engine"] = {
        "health": health,
        "status": _safe_status(health),
        "reconciliation": reconciliation,
        "quality": {
            "timestamp": quality.get("timestamp"),
            "overall_score": quality.get("overall_score"),
            "issues": quality.get("issues", []),
            "channels": quality.get("channels", {}),
        },
    }

    # Topology summary + model status (read-only; failures degrade gracefully).
    try:
        topology = await client.topology()
    except Exception as err:  # noqa: BLE001
        topology = None
        diagnostics["last_errors"].append(f"topology fetch failed: {err}")

    if isinstance(topology, dict):
        channels = normalise_channels(topology)
        diagnostics["topology"] = {
            "version": topology.get("version"),
            "status": topology.get("status"),
            "channel_count": len(channels),
            "device_count": len(topology.get("devices") or []),
            "channels": channels,
            "notes": topology.get("notes", []),
            "assignments": config.get(CONF_CT_ASSIGNMENTS, {}),
        }

    diagnostics["models"] = {}
    results = (coordinator.data.results if coordinator.data else {}) or {}
    for device_id in results:
        try:
            model_payload = await client.models(device_id)
        except Exception:  # noqa: BLE001
            diagnostics["models"][device_id] = {"error": "unavailable"}
            continue
        diagnostics["models"][device_id] = _summarise_models(model_payload)

    # Last successful processing time comes from the engine itself so a
    # diagnostics download still shows it if the coordinator is stale.
    if not diagnostics["coordinator"].get("last_success"):
        diagnostics["coordinator"]["last_success"] = health.get("inference_last_run_at")

    return diagnostics


def _safe_status(health: dict[str, Any]) -> dict[str, Any]:
    """Whitelist health fields; never echo unknown or secret-bearing keys."""
    keys = (
        "status",
        "contract_version",
        "engine_version",
        "database",
        "ha_connected",
        "ha_last_event_at",
        "uptime_seconds",
        "queue_depth",
        "inference_last_run_at",
        "learning_active",
    )
    return {key: health.get(key) for key in keys if key in health}


def _summarise_models(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"raw_type": type(payload).__name__}
    active = payload.get("active")
    summary: dict[str, Any] = {
        "active": {
            "model_id": active.get("model_id"),
            "version": active.get("version"),
            "status": active.get("status"),
            "model_type": active.get("model_type"),
            "confidence": active.get("confidence"),
            "sample_count": active.get("sample_count"),
        }
        if isinstance(active, dict)
        else None,
    }
    versions = payload.get("versions")
    if isinstance(versions, list):
        summary["versions"] = [
            {
                "version": item.get("version"),
                "status": item.get("status"),
                "created_at": item.get("created_at"),
                "sample_count": item.get("sample_count"),
            }
            for item in versions
            if isinstance(item, dict)
        ][:10]
    return summary
