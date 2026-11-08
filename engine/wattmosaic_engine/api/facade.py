"""The ``EngineFacade`` protocol and its fallbacks.

The protocol is intentionally narrow: it mirrors the HTTP surface one-to-one in
snake_case.  Every method may return either a value or a coroutine; the server
awaits the result, so synchronous backends work too.

The return types are the authoritative contract models wherever one exists
(``HealthResponse``, ``SnapshotResponse``, ``Topology``, ...).  Methods without a
contract model return plain JSON-safe dicts/lists.

``NullFacade`` answers every call with :class:`FacadeUnavailable`, which the
server turns into HTTP 503 + ``ErrorResponse``.  API tests use it to prove the
transport works standalone, and the real engine replaces it at startup.
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol, runtime_checkable

from ..contracts import CONTRACT_VERSION, ServiceCommand, ServiceResult

ENGINE_VERSION = "0.1.2"


class FacadeUnavailable(RuntimeError):
    """Raised when a facade method cannot be served (→ HTTP 503)."""


class NotFound(LookupError):
    """Raised when a requested resource does not exist (→ HTTP 404)."""


class InvalidRequest(ValueError):
    """Raised for a semantically invalid request (→ HTTP 422)."""


async def maybe_await(value: Any) -> Any:
    """Await ``value`` when it is awaitable, otherwise return it unchanged."""
    if inspect.isawaitable(value):
        return await value
    return value


def _tier_label(tier: Any) -> str:
    """Human-readable label from a tier value (``tier_3_measured`` -> ``Measured``)."""
    text = str(getattr(tier, "value", tier) or "")
    for prefix in ("tier_0_", "tier_1_", "tier_2_", "tier_3_", "tier_4_"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.replace("_", " ").strip().capitalize() or "Unknown"


def confidence_tier_vocabulary() -> dict[str, Any]:
    """The static, ordered confidence-tier vocabulary served by the API.

    Clients should not hard-code tier names or descriptions: the engine owns the
    vocabulary so a label can never drift from the grader.  Descriptions come
    from :mod:`wattmosaic_engine.disaggregation.tiers`; the import is local
    because the inference package is optional for a transport-only deployment.

    The payload always reports ``calibrated: false``: a tier is an evidence
    *grade*, never a probability.
    """
    from ..contracts import TIER_RANK

    try:
        from ..disaggregation.tiers import TIER_DESCRIPTION
    except Exception:  # noqa: BLE001 - optional backend
        TIER_DESCRIPTION = {}

    tiers: list[dict[str, Any]] = []
    for tier, rank in sorted(TIER_RANK.items(), key=lambda item: item[1]):
        tiers.append(
            {
                "tier": str(getattr(tier, "value", tier)),
                "rank": int(rank),
                "label": _tier_label(tier),
                "description": str(TIER_DESCRIPTION.get(tier, "")),
                "calibrated": False,
            }
        )
    return {
        "tiers": tiers,
        "calibrated": False,
        "note": (
            "a tier is an ordered evidence grade, not a calibrated probability; "
            "the numeric confidence score remains the finer-grained estimator"
        ),
    }


@runtime_checkable
class EngineFacade(Protocol):
    """Everything the HTTP layer needs from the engine."""

    def health(self) -> Any: ...

    def status(self) -> Any: ...

    def snapshot(self) -> Any: ...

    def devices(self) -> Any: ...

    def device(self, device_id: str) -> Any: ...

    def confidence_tiers(self) -> Any: ...

    def topology(self) -> Any: ...

    def set_topology(self, topology: Any) -> Any: ...

    def confirm_topology(self) -> Any: ...

    def quality(self) -> Any: ...

    def diagnostics(self) -> Any: ...

    def models(self, device_id: str) -> Any: ...

    def command(self, command: ServiceCommand) -> Any: ...

    def export(self, device_id: str) -> Any: ...

    def metrics(self) -> Any: ...


class NullFacade:
    """A facade that is not wired to an engine.

    Used before startup finishes and in transport tests.  Every method raises
    :class:`FacadeUnavailable`; ``health`` reports a degraded engine rather than
    failing so health checks can distinguish "not ready" from "broken".
    """

    def __init__(self, reason: str = "inference backend not installed") -> None:
        self.reason = reason

    def health(self) -> dict[str, Any]:
        return {
            "status": "degraded",
            "contract_version": CONTRACT_VERSION,
            "engine_version": ENGINE_VERSION,
            "database": "unavailable",
            "ha_connected": False,
            "ha_last_event_at": None,
            "uptime_seconds": 0.0,
            "queue_depth": 0,
            "inference_last_run_at": None,
            "learning_active": False,
            "details": {"facade": "null", "reason": self.reason},
        }

    def _unavailable(self, *_: Any, **__: Any) -> Any:
        raise FacadeUnavailable(self.reason)

    def status(self) -> Any:
        return self._unavailable()

    def snapshot(self) -> Any:
        return self._unavailable()

    def devices(self) -> Any:
        return self._unavailable()

    def device(self, device_id: str) -> Any:
        return self._unavailable()

    def confidence_tiers(self) -> Any:
        return self._unavailable()

    def topology(self) -> Any:
        return self._unavailable()

    def set_topology(self, topology: Any) -> Any:
        return self._unavailable()

    def confirm_topology(self) -> Any:
        return self._unavailable()

    def quality(self) -> Any:
        return self._unavailable()

    def diagnostics(self) -> Any:
        return self._unavailable()

    def models(self, device_id: str) -> Any:
        return self._unavailable()

    def command(self, command: ServiceCommand) -> Any:
        return self._unavailable()

    def export(self, device_id: str) -> Any:
        return self._unavailable()

    def metrics(self) -> Any:
        return self._unavailable()


class StubFacade(NullFacade):
    """A deterministic in-memory facade for tests and demos.

    ``StubFacade`` implements every read endpoint with small, fixed data so the
    HTTP surface can be exercised end to end without an inference engine.  It is
    never used in production: ``main`` wires the real ``EngineCore``.
    """

    def __init__(self) -> None:
        super().__init__("stub facade")
        self._topology = {
            "version": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
            "status": "candidate",
            "channels": [
                {
                    "ct_id": "ct_l1",
                    "entity_id": "sensor.example_l1_power",
                    "name": "L1 main",
                    "ct_type": "main",
                    "phase": "l1",
                    "polarity": "normal",
                    "topology_status": "candidate",
                }
            ],
            "devices": [
                {
                    "device_id": "stub_device",
                    "name": "Stub load",
                    "dedicated_ct_ids": [],
                    "branch_ct_ids": ["ct_l1"],
                    "topology_status": "candidate",
                }
            ],
            "notes": ["stub topology"],
        }
        self.commands: list[ServiceCommand] = []

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "contract_version": CONTRACT_VERSION,
            "engine_version": ENGINE_VERSION,
            "database": "ok",
            "ha_connected": True,
            "ha_last_event_at": None,
            "uptime_seconds": 0.0,
            "queue_depth": 0,
            "inference_last_run_at": None,
            "learning_active": False,
            "details": {"facade": "stub"},
        }

    def status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "facade": "stub",
            "contract_version": CONTRACT_VERSION,
            "engine_version": ENGINE_VERSION,
            "uptime_seconds": 0.0,
            "learning_active": False,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "reconciliation": {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "topology_version": 1,
                "main_power_w": 0.0,
                "unknown_residual_w": 0.0,
            },
            "results": [],
            "quality": None,
        }

    def devices(self) -> list[dict[str, Any]]:
        return []

    def device(self, device_id: str) -> dict[str, Any]:
        raise NotFound(f"device {device_id!r} not found")

    def confidence_tiers(self) -> dict[str, Any]:
        return confidence_tier_vocabulary()

    def topology(self) -> dict[str, Any]:
        return dict(self._topology)

    def set_topology(self, topology: Any) -> dict[str, Any]:
        data = topology.model_dump(mode="json") if hasattr(topology, "model_dump") else dict(topology)
        data.setdefault("status", "candidate")
        self._topology = data
        return data

    def confirm_topology(self) -> dict[str, Any]:
        self._topology["status"] = "confirmed"
        return dict(self._topology)

    def quality(self) -> dict[str, Any]:
        return {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "overall_score": 1.0,
            "channels": {},
            "issues": [],
        }

    def diagnostics(self) -> dict[str, Any]:
        return {"generated_at": "2026-01-01T00:00:00+00:00", "facade": "stub", "ok": True}

    def models(self, device_id: str) -> dict[str, Any]:
        return {"device_id": device_id, "active": None, "versions": []}

    def command(self, command: ServiceCommand) -> ServiceResult:
        self.commands.append(command)
        return ServiceResult(ok=True, command=command.command, detail="stub")

    def export(self, device_id: str) -> dict[str, Any]:
        return {"device_id": device_id, "exported_at": "2026-01-01T00:00:00+00:00", "data": {}}

    def metrics(self) -> str:
        return "# HELP wattmosaic_up Engine process is up\n# TYPE wattmosaic_up gauge\nwattmosaic_up 1\n"


__all__ = [
    "ENGINE_VERSION",
    "EngineFacade",
    "FacadeUnavailable",
    "InvalidRequest",
    "NotFound",
    "NullFacade",
    "StubFacade",
    "confidence_tier_vocabulary",
    "maybe_await",
]
