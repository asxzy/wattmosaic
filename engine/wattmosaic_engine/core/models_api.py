"""Model facade: versions, summaries, freeze/unfreeze and service commands.

Owned by L1 after X03 (model registry)."""

from __future__ import annotations

from typing import Any

from ..api.facade import InvalidRequest, NotFound
from ..contracts import DeviceConfiguration, DeviceConfigurationSet, ServiceCommand, ServiceResult


class ModelsApiMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    def _v3_registry(self) -> Any | None:
        """Return the v3 model registry, constructing one lazily from the DB.

        The v3 registry (L1) is separate from the legacy ``model_registry``
        used by inference.  Building it here keeps the HTTP read path working
        on a fresh ``EngineCore`` without changing composition in ``main.py``.
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
        """Model versions come from the shared registry when it is available.

        The registry is created by the inference adapter, so this never
        constructs a second one; it only reuses ``self.model_registry``.  Falls
        back to the database for a facade used without a backend.

        On the v3 path (L1) a subject with append-only v3 versions gets the v3
        payload — ``PowerModelVersion[]`` plus its event log — so ``GET
        /models/{subject_id}`` is the single model-read endpoint.  Legacy
        devices with only old rows keep the original shape until Z2.
        """
        v3 = self._v3_registry()
        if v3 is not None:
            try:
                versions_v3 = v3.versions(device_id)
            except Exception:  # noqa: BLE001
                versions_v3 = []
            if versions_v3:
                active_v3 = v3.active_version(device_id)
                return {
                    "subject_id": device_id,
                    "model_set_id": v3.model_set_id,
                    "active": self._model_dump(active_v3) if active_v3 else None,
                    "versions": [self._model_dump(v) for v in versions_v3],
                    "events": v3.events(device_id),
                    # Legacy client keys, preserved so existing panel code keeps
                    # working during the staged rollout (removed at Z1).
                    "device_id": device_id,
                }
        registry = self.model_registry
        try:
            if registry is not None:
                active = self._model_dump(registry.active(device_id))
                versions = [self._model_dump(v) for v in registry.versions(device_id)]
            else:
                active = self.db.active_model(device_id)
                versions = self.db.model_versions(device_id)
        except Exception:  # noqa: BLE001
            active, versions = None, []
        if active is None and not versions:
            raise NotFound(f"no models for device {device_id!r}")
        return {"device_id": device_id, "active": active, "versions": versions}

    def models_summary(self) -> dict[str, Any]:
        topology = self._active_topology()
        out: dict[str, Any] = {}
        for dev in topology.devices if topology else []:
            active = self._active_model_summary(dev.device_id)
            if active:
                out[dev.device_id] = active
        return {"devices": out, "count": len(out)}

    @staticmethod
    def _model_dump(value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "as_dict"):
            return value.as_dict()
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        return value

    def _active_model_summary(self, device_id: str) -> dict[str, Any] | None:
        try:
            row = self.db.active_model(device_id)
        except Exception:  # noqa: BLE001
            return None
        if not row:
            return None
        return {
            "model_type": row.get("model_type"),
            "version": row.get("version"),
            "status": row.get("status"),
            "confidence": row.get("confidence"),
            "sample_count": row.get("sample_count"),
            "trained_to": row.get("trained_to"),
        }

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
        if name == "set_device_configuration":
            return self._set_device_configuration(command)
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
            self.db.set_model_frozen(command.device_id, frozen)
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

    def _set_device_configuration(self, command: ServiceCommand) -> ServiceResult:
        """Persist one device's declarative configuration and project it.

        An empty ``parameters`` mapping means *remove* this device's
        configuration: deletion must clear the topology projection too, otherwise
        a stale monitoring level or mode entity would outlive the declaration.
        """
        from ..device_configuration import project_configuration

        name = command.command
        if not command.device_id:
            raise InvalidRequest("set_device_configuration requires device_id")
        device_id = command.device_id

        topology = self._active_topology()
        if topology is None:
            raise NotFound("no topology to project the device configuration onto")

        current = self.db.load_device_configurations()
        by_device = current.by_device()
        parameters = dict(command.parameters or {})

        if not parameters:
            by_device.pop(device_id, None)
            new_set = DeviceConfigurationSet(
                version=current.version + 1,
                updated_at=self._clock(),
                devices=list(by_device.values()),
            )
            self.db.save_device_configurations(new_set)
            projected = project_configuration(topology, new_set)
            self.db.save_topology(projected, note=f"api: remove configuration {device_id}")
            self.topology_cache = projected
            self.db.log_system_event(
                "info", "configuration", f"set_device_configuration {device_id} (removed)"
            )
            return ServiceResult(
                ok=True,
                command=name,
                data={"device_id": device_id, "removed": True},
            )

        # ``device_id`` comes from the command, never from the payload: a caller
        # must not be able to write another device's row through ``parameters``,
        # so any copy supplied there is dropped before validation.
        payload = {k: v for k, v in parameters.items() if k != "device_id"}
        try:
            config = DeviceConfiguration.model_validate({"device_id": device_id, **payload})
        except Exception as exc:  # noqa: BLE001 - invalid input is a result, not a crash
            return ServiceResult(
                ok=False, command=name, detail=f"invalid configuration: {exc}"
            )

        by_device[device_id] = config
        new_set = DeviceConfigurationSet(
            version=current.version + 1,
            updated_at=self._clock(),
            devices=list(by_device.values()),
        )
        self.db.save_device_configurations(new_set)
        projected = project_configuration(topology, new_set)
        self.db.save_topology(projected, note=f"api: set configuration {device_id}")
        self.topology_cache = projected
        self.db.log_system_event("info", "configuration", f"set_device_configuration {device_id}")
        return ServiceResult(
            ok=True,
            command=name,
            data={
                "device_id": device_id,
                "configuration": config.model_dump(mode="json"),
                "topology_version": projected.version,
            },
        )
