"""Model facade: versions, summaries, freeze/unfreeze and service commands.

Owned by L1 after X03 (model registry)."""

from __future__ import annotations

from typing import Any

from ..api.facade import InvalidRequest, NotFound
from ..contracts import ServiceCommand, ServiceResult


class ModelsApiMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    def _v3_registry(self) -> Any | None:
        """Return the v3 model registry, constructing one lazily from the DB.

        The event loop owns ``model_registry_v3``; this builds a read-side
        registry lazily so the HTTP read path works on a fresh ``EngineCore``.
        """
        registry = getattr(self, "_model_registry_v3", None)
        if registry is not None:
            return registry
        try:
            from ..models.registry import DatabaseModelRepository, Registry

            registry = Registry(DatabaseModelRepository(self.db), clock=self._clock)
        except Exception:  # noqa: BLE001 - optional model layer
            return None
        self._model_registry_v3 = registry
        return registry

    def models_v3(self, subject_id: str) -> dict[str, Any]:
        """Return a subject's append-only model versions and lifecycle events.

        This backs ``GET /models/{subject_id}`` (interfaces §9).  A subject with
        no versions is a 404 — never an empty 200 (contract rule).
        """
        registry = self._v3_registry()
        if registry is None:
            raise NotFound(f"no models for subject {subject_id!r}")
        versions = registry.versions(subject_id)
        if not versions:
            raise NotFound(f"no models for subject {subject_id!r}")
        active = registry.active_version(subject_id)
        return {
            "subject_id": subject_id,
            "model_set_id": registry.model_set_id,
            "active": self._model_dump(active) if active else None,
            "versions": [self._model_dump(v) for v in versions],
            "events": registry.events(subject_id),
        }

    def models(self, device_id: str) -> dict[str, Any]:
        """``GET /models/{subject_id}``: the v3 append-only versions and events.

        A subject with no versions is a 404 — never an empty 200.
        """
        return self.models_v3(device_id)

    @staticmethod
    def _model_dump(value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        return value

    async def command(self, command: ServiceCommand) -> ServiceResult:
        """Handle a ``ServiceCommand``.

        Destructive operations are refused with HTTP 409 semantics handled by the
        facade raising ``InvalidRequest`` only for bad input; unsupported
        commands report ``ok=False`` rather than crashing the API.
        """
        name = command.command
        if name == "discover_placements":
            lookback = command.parameters.get("lookback_hours", 24.0)
            try:
                hours = float(lookback)
            except (TypeError, ValueError):
                raise InvalidRequest("lookback_hours must be a number") from None
            apply = command.parameters.get("apply")
            include_groups = command.parameters.get("include_groups", True)
            max_candidates = command.parameters.get("max_candidates", 200)
            # The bounds and the type checks live in ``discover_placements`` so the
            # route and the command cannot disagree; only the coercion that turns
            # JSON scalars into the declared parameter types happens here, and a
            # bad value is the caller's error, not a crash.
            if not isinstance(include_groups, bool):
                raise InvalidRequest("include_groups must be a boolean")
            if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
                raise InvalidRequest("max_candidates must be an integer")
            data = await self.discover_placements(
                lookback_hours=hours,
                apply=None if apply is None else bool(apply),
                include_groups=include_groups,
                max_candidates=max_candidates,
            )
            return ServiceResult(ok=bool(data.get("ok")), command=name, data=data)
        if name in ("start_learning", "resume_learning", "pause_learning"):
            active = name != "pause_learning"
            backend = self.inference_backend
            setter = getattr(backend, "set_learning_active", None)
            if callable(setter):
                setter(active)
            if active and (backend is None or getattr(backend, "learning", None) is None):
                return ServiceResult(
                    ok=False,
                    command=name,
                    detail="learning pipeline is unavailable; enable it and restart the engine",
                )
            self.learning_active = active
            self.db.log_system_event(
                "info", "learning", f"learning {'resumed' if active else 'paused'} via API"
            )
            return ServiceResult(
                ok=True,
                command=name,
                detail=f"learning {'active' if active else 'paused'}",
            )
        if name == "run_diagnostics":
            return ServiceResult(ok=True, command=name, data=self.diagnostics())
        if name == "export_device_data":
            if not command.device_id:
                raise InvalidRequest("export_device_data requires device_id")
            try:
                data = self.export(command.device_id)
            except NotFound as exc:
                return ServiceResult(ok=False, command=name, detail=str(exc))
            return ServiceResult(ok=True, command=name, data=data)
        if name in ("freeze_model", "unfreeze_model"):
            if not command.device_id:
                raise InvalidRequest(f"{name} requires device_id")
            frozen = name == "freeze_model"
            registry = self._v3_registry()
            if registry is None:
                return ServiceResult(ok=False, command=name, detail="model registry unavailable")
            if frozen:
                registry.freeze(command.device_id)
            else:
                registry.unfreeze(command.device_id)
            self.db.log_system_event("info", "models", f"{name} {command.device_id}")
            return ServiceResult(
                ok=True, command=name, detail=f"model {'frozen' if frozen else 'unfrozen'}"
            )
        if name in ("rebuild_topology",):
            return ServiceResult(
                ok=False,
                command=name,
                detail="rebuild_topology requires the inference backend; no automatic rebuild performed",
            )
        if name in (
            "accept_sample",
            "reject_sample",
            "rebuild_model",
            "reset_device_model",
        ):
            return ServiceResult(
                ok=False,
                command=name,
                detail=f"{name} requires the learning backend, which is not installed",
            )
        return ServiceResult(ok=False, command=name, detail="unsupported command")
