"""Pure service-call validation for the WattMosaic integration.

Kept free of Home Assistant imports so the destructive-operation guard rails
are unit-tested directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import (
    APPLY_CONFIRMATION_SERVICES,
    DESTRUCTIVE_SERVICES,
    SERVICE_ACCEPT_SAMPLE,
    SERVICE_DISCOVER_PLACEMENTS,
    SERVICE_EXPORT_DEVICE_DATA,
    SERVICE_FREEZE_MODEL,
    SERVICE_REBUILD_MODEL,
    SERVICE_REBUILD_TOPOLOGY,
    SERVICE_REJECT_SAMPLE,
    SERVICE_RESET_DEVICE_MODEL,
    SERVICE_RUN_DIAGNOSTICS,
    SERVICE_UNFREEZE_MODEL,
    SERVICES,
)

#: Services that address one device.
DEVICE_SERVICES: frozenset[str] = frozenset(
    {
        SERVICE_ACCEPT_SAMPLE,
        SERVICE_REJECT_SAMPLE,
        SERVICE_REBUILD_MODEL,
        SERVICE_FREEZE_MODEL,
        SERVICE_UNFREEZE_MODEL,
        SERVICE_RESET_DEVICE_MODEL,
        SERVICE_EXPORT_DEVICE_DATA,
    }
)

#: Services that address one learning sample (and also need the device).
SAMPLE_SERVICES: frozenset[str] = frozenset({SERVICE_ACCEPT_SAMPLE, SERVICE_REJECT_SAMPLE})

#: Optional device scope.
OPTIONAL_DEVICE_SERVICES: frozenset[str] = frozenset({SERVICE_REBUILD_TOPOLOGY})


class ServiceValidationError(ValueError):
    """Raised when a service call is malformed.

    ``key`` is a translation key from ``strings.json`` under
    ``exceptions.<key>``; ``str(error)`` is safe to show to the user.
    """

    def __init__(self, key: str, message: str) -> None:
        self.key = key
        self.message = message
        super().__init__(message)


class ConfirmationRequired(ServiceValidationError):
    """Raised for destructive services without ``confirm: true``."""

    def __init__(self, service: str) -> None:
        self.service = service
        super().__init__(
            "confirmation_required",
            f"{service} is destructive and requires 'confirm: true'",
        )


def _clean_id(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def validate_service_call(
    service: str,
    data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate a service call and return a normalised payload.

    Returns ``{"device_id", "sample_id", "parameters", "confirm"}``.

    Raises
    ------
    ServiceValidationError
        Unknown service, missing/invalid device or sample id.
    ConfirmationRequired
        Destructive service without an explicit ``confirm: true``.
    """
    if service not in SERVICES:
        raise ServiceValidationError("unknown_service", f"unknown service: {service}")
    data = dict(data or {})

    confirm = data.get("confirm")
    if service in DESTRUCTIVE_SERVICES and confirm is not True:
        raise ConfirmationRequired(service)

    parameters = data.get("parameters") or {}
    if not isinstance(parameters, Mapping):
        raise ServiceValidationError("invalid_parameters", "'parameters' must be a mapping")

    if (
        service in APPLY_CONFIRMATION_SERVICES
        and parameters.get("apply") is True
        and confirm is not True
    ):
        # Analysis is read-only and always allowed; only writing devices needs
        # consent, so the gate follows the parameter rather than the service.
        raise ConfirmationRequired(service)

    device_id = _clean_id(data.get("device_id"))
    sample_id = _clean_id(data.get("sample_id"))

    if service in DEVICE_SERVICES and not device_id:
        raise ServiceValidationError(
            "device_required", f"{service} requires 'device_id'"
        )

    if service in SAMPLE_SERVICES and not sample_id:
        raise ServiceValidationError(
            "sample_required", f"{service} requires 'sample_id'"
        )

    if device_id is not None and len(device_id) > 256:
        raise ServiceValidationError("invalid_device_id", "device_id is too long")
    if sample_id is not None and len(sample_id) > 256:
        raise ServiceValidationError("invalid_sample_id", "sample_id is too long")

    parameters = data.get("parameters") or {}
    if not isinstance(parameters, Mapping):
        raise ServiceValidationError("invalid_parameters", "'parameters' must be a mapping")

    return {
        "device_id": device_id,
        "sample_id": sample_id,
        "parameters": dict(parameters),
        "confirm": confirm is True,
    }


def service_needs_device(service: str) -> bool:
    return service in DEVICE_SERVICES


def service_is_destructive(service: str) -> bool:
    return service in DESTRUCTIVE_SERVICES


def service_returns_response(service: str) -> bool:
    return service in (
        SERVICE_RUN_DIAGNOSTICS,
        SERVICE_EXPORT_DEVICE_DATA,
        SERVICE_DISCOVER_PLACEMENTS,
    )


def build_command_payload(
    service: str,
    validated: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the ``ServiceCommand`` body sent to ``POST /commands``.

    ``confirm`` is intentionally *not* forwarded: the engine trusts the
    integration's authenticated request, and the confirmation is a UI safety
    gate.  It is forwarded inside ``parameters`` only when the caller provided
    extra parameters.
    """
    payload: dict[str, Any] = {"command": service, "parameters": dict(validated.get("parameters") or {})}
    device_id = validated.get("device_id")
    sample_id = validated.get("sample_id")
    if device_id:
        payload["device_id"] = device_id
    if sample_id:
        payload["sample_id"] = sample_id
    return payload


def service_result_summary(result: Mapping[str, Any] | None) -> str:
    """Human-readable one-line summary of an engine ``ServiceResult``."""
    if not isinstance(result, Mapping):
        return "no result"
    ok = bool(result.get("ok"))
    detail = result.get("detail")
    text = str(detail).strip() if detail else ""
    if not text:
        text = "completed" if ok else "failed"
    return f"{'ok' if ok else 'error'}: {text}"
