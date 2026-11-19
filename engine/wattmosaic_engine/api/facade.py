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
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from .. import __version__ as ENGINE_VERSION
from ..contracts import (
    CONTRACT_VERSION,
    ServiceCommand,
    ServiceResult,
    Topology,
    TopologyRelation,
)
from ..models.view import resolve_model
from .protocols import (
    DiagnosticsProvider,
    DiscoveryProvider,
    MappingProvider,
    ModelsProvider,
    SnapshotProvider,
    TopologyProvider,
)


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


def _relation_from_raw(raw: Any) -> TopologyRelation | None:
    """Best-effort parse of a stored/serialised relation into the contract."""
    if isinstance(raw, TopologyRelation):
        return raw
    if not isinstance(raw, Mapping):
        return None
    try:
        return TopologyRelation.model_validate(dict(raw))
    except Exception:  # noqa: BLE001 - one bad edge never fails the whole read
        return None


async def _real_device_sources(
    facade: EngineFacade, topology: Topology
) -> list[list[dict[str, Any]]]:
    """Collect real-device records for the matrix row axis, additively.

    Two sources are available without fabricating anything:

    * **HA registry devices** — the engine core may expose ``ha_device_registry``
      returning ``{"devices": [...]}`` from the already-cached
      ``HASource.registries()``.  Each entry's name is ``name_by_user`` or
      ``name``.  A facade without this hook (stub/null/older backend) simply
      contributes nothing here.
    * **DeviceConfiguration-backed devices** — topology rows whose metadata
      carries the configuration/materialisation markers written by
      ``project_configuration``.  These are real appliances even without HA.

    HA registry devices are included only when the registry data is actually
    available; no rows are invented when it is not.
    """
    sources: list[list[dict[str, Any]]] = []

    registry_reader = getattr(facade, "ha_device_registry", None)
    if registry_reader is not None:
        try:
            payload = await maybe_await(registry_reader())
            entries = (
                payload.get("devices", [])
                if isinstance(payload, Mapping)
                else payload
            )
            devices: list[dict[str, Any]] = []
            for entry in entries or []:
                if not isinstance(entry, Mapping):
                    continue
                device_id = entry.get("id") or entry.get("device_id")
                if not device_id:
                    continue
                devices.append(
                    {
                        "device_id": str(device_id),
                        "name": entry.get("name_by_user") or entry.get("name"),
                    }
                )
            if devices:
                sources.append(devices)
        except Exception:  # noqa: BLE001 - registry is optional; never fail the read
            pass

    config_markers = (
        "device_config",
        "materialised_from_config",
        "monitoring_level",
        "power_behavior",
        "connectivity",
    )
    configured: list[dict[str, Any]] = []
    for device in topology.devices:
        metadata = device.metadata or {}
        if any(key in metadata for key in config_markers):
            configured.append({"device_id": device.device_id, "name": device.name})
    if configured:
        sources.append(configured)

    return sources


async def derive_mapping_matrix(facade: EngineFacade) -> dict[str, Any]:
    """Project the mapping matrix from a facade's *current* state.

    This is a pure read-time projection (REQ-MAPPING-001/005): it calls only the
    facade's existing read methods — ``topology``, ``relations`` and ``models``
    — and hands their current values to ``build_mapping_matrix``.  It writes no
    storage, takes no sample and keeps no per-sample snapshot, so repeated calls
    against unchanged state yield a byte-identical matrix.

    ``reader_configuration_version`` is derived from the topology version: the
    topology *is* the reader configuration (its channels are the readers), so a
    stable, non-invented version string is available from its own version id.
    No association/model data that is absent is fabricated: an empty topology
    yields empty cells and a zeroed coverage block.

    ``dev_fridge``-style model reads come back as dicts; ``resolve_model``
    normalises them to ``ModelView`` without constructing a second registry.
    """
    from ..disaggregation.mapping import MappingEvidence, build_mapping_matrix

    topology_obj = await maybe_await(facade.topology())
    if not isinstance(topology_obj, Topology):
        try:
            topology_obj = Topology.model_validate(topology_obj)
        except Exception:  # noqa: BLE001
            topology_obj = Topology()
    topology: Topology = topology_obj

    associations: list[TopologyRelation] = []
    relations_reader = getattr(facade, "relations", None)
    if relations_reader is not None:
        try:
            payload = await maybe_await(relations_reader("device_ct_association"))
            rows = (
                payload.get("kinds", {}).get("device_ct_association", [])
                if isinstance(payload, Mapping)
                else []
            )
            for raw in rows:
                relation = _relation_from_raw(raw)
                if relation is not None:
                    associations.append(relation)
        except Exception:  # noqa: BLE001 - a backend without relations still works
            associations = []
    # Relations already declared on the topology are the primary evidence and
    # are read by build_mapping_matrix directly; stored ones are additive.

    model_views: dict[str, Any] = {}
    models_reader = getattr(facade, "models", None)
    if models_reader is not None:
        for device in topology.devices:
            try:
                payload = await maybe_await(models_reader(device.device_id))
                active = payload.get("active") if isinstance(payload, Mapping) else None
                if active is None:
                    continue
                view = resolve_model({device.device_id: active}, device.device_id)
                if view is not None:
                    model_views[device.device_id] = view
            except Exception:  # noqa: BLE001 - per-device isolation
                continue

    evidence = MappingEvidence(associations=tuple(associations))
    device_sources = await _real_device_sources(facade, topology)
    matrix = build_mapping_matrix(
        topology,
        models=model_views,
        evidence=evidence,
        reader_configuration_version=f"topology-{topology.version}",
        topology_version=str(topology.version),
        device_sources=device_sources,
    )
    return matrix.as_dict()


@runtime_checkable
class EngineFacade(
    SnapshotProvider,
    TopologyProvider,
    MappingProvider,
    ModelsProvider,
    DiscoveryProvider,
    DiagnosticsProvider,
    Protocol,
):
    """Everything the HTTP layer needs from the engine.

    This is the composition of the six per-area protocols in
    :mod:`api.protocols`.  It declares no methods of its own, so existing
    facades and test doubles that satisfy the old monolithic protocol keep
    working unchanged.
    """


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

    def panel_tree(self, *, include_nesting: bool = False) -> Any:
        return self._unavailable()

    def nesting_candidates(self, *, hours: float = 12.0) -> Any:
        return self._unavailable()

    def quality(self) -> Any:
        return self._unavailable()

    def diagnostics(self) -> Any:
        return self._unavailable()

    def circuits(self) -> Any:
        return self._unavailable()

    def relations(self, kind: str | None = None) -> Any:
        return self._unavailable()

    def inference_records(self, device_id: str, *, limit: int = 200) -> Any:
        return self._unavailable()

    def residual_components(self, *, hours: float = 24.0, limit: int = 500) -> Any:
        return self._unavailable()

    def isolation_status(self) -> Any:
        return self._unavailable()

    def models(self, device_id: str) -> Any:
        return self._unavailable()

    def command(self, command: ServiceCommand) -> Any:
        return self._unavailable()

    def discover_placements(
        self,
        *,
        lookback_hours: float = 24.0,
        apply: bool | None = None,
        include_groups: bool = True,
        max_candidates: int = 200,
    ) -> Any:
        """No engine, so nothing was analysed and nothing may be applied.

        Returning a shaped answer rather than raising keeps the transport usable
        before startup finishes: a caller can render "not available" without
        special-casing one endpoint.
        """
        return {
            "ok": False,
            "detail": "not available",
            "accepted": [],
            "refused": {},
            "groups": [],
        }

    def export(self, device_id: str) -> Any:
        return self._unavailable()

    def metrics(self) -> Any:
        return self._unavailable()

    def reset_database(self) -> Any:
        raise FacadeUnavailable(self.reason)

    def mapping_matrix(self) -> Any:
        """Optional additive hook: ``None`` means "derive from current state".

        The HTTP layer falls back to :func:`derive_mapping_matrix`, so a facade
        that predates this method still serves the route without any change.
        """
        return None


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

    def panel_tree(self, *, include_nesting: bool = False) -> dict[str, Any]:
        """Build the tree from the stub topology (no engine, no history)."""
        from ..contracts import Topology
        from ..mapping.display_tree import build_display_tree

        return build_display_tree(Topology.model_validate(self._topology))

    def nesting_candidates(self, *, hours: float = 12.0) -> dict[str, Any]:
        return {"candidates": [], "reason": "stub facade has no stored history"}

    def quality(self) -> dict[str, Any]:
        return {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "overall_score": 1.0,
            "channels": {},
            "issues": [],
        }

    def diagnostics(self) -> dict[str, Any]:
        return {"generated_at": "2026-01-01T00:00:00+00:00", "facade": "stub", "ok": True}

    def circuits(self) -> list[dict[str, Any]]:
        return []

    def relations(self, kind: str | None = None) -> dict[str, Any]:
        return {"kinds": {}, "counts": {}, "requires_confirmation": []}

    def inference_records(self, device_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return []

    def residual_components(self, *, hours: float = 24.0, limit: int = 500) -> dict[str, Any]:
        return {"window_hours": float(hours), "count": 0, "components": []}

    def isolation_status(self) -> dict[str, Any]:
        return {
            "source": "stub",
            "counts": {"analysed": 0, "isolated": 0, "contaminated": 0, "insufficient_data": 0},
            "verdicts": [],
        }

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
    "derive_mapping_matrix",
    "maybe_await",
]
