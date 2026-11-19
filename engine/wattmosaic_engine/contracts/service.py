"""HTTP service envelopes (contract 3.0).

The health probe response, the error envelope and the integration's service
command/result pair.  These are transport shapes shared by the engine's HTTP
layer and the Home Assistant integration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from .. import __version__ as ENGINE_VERSION
from .foundation import CONTRACT_VERSION, Model, utcnow

__all__: list[str] = [
    "ErrorResponse",
    "HealthResponse",
    "ServiceCommand",
    "ServiceResult",
]


class HealthResponse(Model):
    status: Literal["ok", "degraded", "error"] = "ok"
    contract_version: str = CONTRACT_VERSION
    # A factory, not a plain default: pydantic writes a plain default into the
    # generated JSON schema, so the shipped contract would embed the version of
    # whichever tree last ran `export_schemas.py`.  CI bumps the version on
    # every push to main and then asserts `git diff --exit-code data/schemas`,
    # so a version *value* in the schema turns every auto-release red.  The
    # factory still fills the runtime field at response time; it just keeps the
    # generated schema free of a value that changes without the contract
    # changing.
    engine_version: str = Field(default_factory=lambda: ENGINE_VERSION)
    database: str = "unknown"
    ha_connected: bool = False
    ha_last_event_at: datetime | None = None
    uptime_seconds: float = 0.0
    queue_depth: int = 0
    inference_last_run_at: datetime | None = None
    learning_active: bool = True
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(Model):
    error: str
    detail: str | None = None
    retryable: bool = False


class ServiceCommand(Model):
    """Command sent from the HA integration to the engine."""

    command: Literal[
        "start_learning",
        "pause_learning",
        "resume_learning",
        "accept_sample",
        "reject_sample",
        "rebuild_model",
        "freeze_model",
        "unfreeze_model",
        "reset_device_model",
        "rebuild_topology",
        "run_diagnostics",
        "export_device_data",
        "discover_placements",
    ]
    device_id: str | None = None
    sample_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    requested_at: datetime = Field(default_factory=utcnow)


class ServiceResult(Model):
    ok: bool
    command: str
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


SCHEMA_EXPORTS: list[tuple[str, type]] = [
    ("health_response", HealthResponse),
    ("error_response", ErrorResponse),
    ("service_command", ServiceCommand),
    ("service_result", ServiceResult),
]
