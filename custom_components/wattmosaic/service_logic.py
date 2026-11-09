"""Pure service-call validation for the WattMosaic integration.

Kept free of Home Assistant imports so the destructive-operation guard rails
are unit-tested directly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .const import (
    APPLY_CONFIRMATION_SERVICES,
    DESTRUCTIVE_SERVICES,
    DOMAIN,
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


def target_device_ids(data: Mapping[str, Any] | None) -> list[str]:
    """Home Assistant device ids supplied through the ``target`` selector.

    ``services.yaml`` documents ``target.device`` support, but Home Assistant
    only expands ``target`` into ``device_id`` for services that declare the
    selector; for these it arrives as a nested mapping.  The values are HA
    *registry* ids (``dabfb08...``), not engine ids, so they are a routing key
    only and must never be forwarded to an engine as a device id.

    Returns an empty list when no target was given, so callers can treat
    "no target" and "target that does not resolve" differently.
    """
    if not isinstance(data, Mapping):
        return []
    target = data.get("target")
    if not isinstance(target, Mapping):
        return []
    raw = target.get("device_id")
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = [str(item) for item in raw]
    else:
        return []
    return [value for value in (_clean_id(item) for item in values) if value]


def identifier_pair(identifier: Any) -> tuple[str | None, str | None]:
    """``(domain, value)`` from a device-registry identifier of any length.

    A registry identifier is documented as a ``(domain, value)`` pair, but Home
    Assistant does not enforce the length: the HomeKit controller stores
    three-element tuples such as ``("homekit", "<id>", "homekit.bridge")``.
    Unpacking ``for domain, value in device.identifiers`` therefore raises
    ``ValueError: too many values to unpack`` for *every* device lookup on an
    installation that has any HomeKit bridge -- which is what happened live,
    turning a routing error into an opaque 500.

    Only the first two elements carry the meaning this integration needs, so
    extra elements are ignored rather than treated as an error.  Malformed or
    non-string entries yield ``(None, None)`` so one bad device cannot break a
    lookup over the whole registry.
    """
    if not isinstance(identifier, (tuple, list)) or len(identifier) < 2:
        return None, None
    domain, value = identifier[0], identifier[1]
    if not isinstance(domain, str) or not isinstance(value, str):
        return None, None
    return domain, value


def match_identifier_owners(
    identifiers: Sequence[Any],
    *,
    device_id: str,
    entries: Sequence[str],
) -> tuple[str | None, list[str]]:
    """Resolve a ``device_id`` against the identifiers this integration published.

    ``identifiers`` is every raw device-registry identifier the integration
    owns; each is normalised through :func:`identifier_pair`, so a three-element
    HomeKit tuple cannot crash the scan.  A published value is
    ``<entry_id>_<engine_device_id>``, so the owning entry is derived from the
    value itself rather than assumed.  Returns ``(exact_entry, candidates)``:

    * ``exact_entry`` is set when ``device_id`` *is* an identifier value (the
      fully qualified form).  This check has to come first: rebuilding a fully
      qualified id as ``<entry_id>_<device_id>`` prefixes it a second time and
      matches nothing, which is exactly how the fully qualified form stopped
      working.  If more than one entry claims it the value is not treated as
      exact, because that would be a guess.
    * ``candidates`` is every entry publishing ``<entry_id>_<device_id>``.  A
      bare engine id lands here, and normally in *both* a stable and a
      development entry, so the caller must decide rather than take the first.
    """
    values: list[str] = []
    for raw in identifiers:
        domain, value = identifier_pair(raw)
        if domain == DOMAIN and value is not None:
            values.append(value)

    if device_id in values:
        owners = [
            entry for entry in entries if device_id.startswith(f"{entry}_")
        ]
        if len(owners) == 1:
            return owners[0], []

    candidates = [entry for entry in entries if f"{entry}_{device_id}" in values]
    return None, candidates


def choose_entry(
    entries: Sequence[str],
    candidates: Sequence[str],
    *,
    device_id: str | None,
) -> str:
    """Choose which configured engine should serve one service call.

    ``entries`` is every loaded config entry; ``candidates`` is the subset whose
    device registry says the call's target belongs to them (already reduced to
    one mechanism by the caller).  With one engine configured there is nothing
    to decide.  With several, the target has to identify exactly one of them.

    The important property is that this **never guesses**.  The previous
    behaviour fell back to whichever entry happened to be first, which meant a
    bare engine device id silently reached the wrong engine: both a stable and a
    development App publish the same engine ids, so ``dev_...`` matched both and
    the first won.  The caller then got a plausible-looking answer from an engine
    that never received the command -- worse than an error, because the mistake
    is invisible.

    Raises
    ------
    ServiceValidationError
        ``engine_not_configured`` when nothing is loaded, ``engine_ambiguous``
        when several engines are loaded and no target was given,
        ``ambiguous_target`` when the target exists in more than one entry, and
        ``unknown_target`` when it exists in none.
    """
    if not entries:
        raise ServiceValidationError("engine_not_configured", "WattMosaic is not configured")

    if len(entries) == 1:
        # A single engine cannot be confused with another.  A bad target is
        # still forwarded so the engine reports it precisely; refusing here
        # would hide which identifier the engine actually knows.
        return entries[0]

    unique = list(dict.fromkeys(candidates))
    if len(unique) == 1:
        return unique[0]

    if len(unique) > 1:
        raise ServiceValidationError(
            "ambiguous_target",
            f"device_id {device_id!r} exists in more than one WattMosaic engine "
            f"({', '.join(unique)}); pass the fully qualified "
            "'<config_entry_id>_<engine_device_id>' or the Home Assistant device",
        )

    if device_id is None:
        raise ServiceValidationError(
            "engine_ambiguous",
            f"{len(entries)} WattMosaic engines are configured; specify device_id "
            "to choose one",
        )

    raise ServiceValidationError(
        "unknown_target",
        f"device_id {device_id!r} does not belong to any WattMosaic engine",
    )


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
