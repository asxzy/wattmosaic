"""Physical Device and Measurement Endpoint lifecycle service (D2).

This module owns the create/retire workflows for Physical Devices and the
endpoint scope confirm/reject workflow that gates MEASURED (DEC-05/06).  All
writes are **append-only**: a scope change adds a config revision and a
``ConfigurationEvent``; nothing is overwritten in place.

It depends only on the ``Database``/repository seam (storage) and the contracts
from K01/K03.  It never imports ``runtime.fakes``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Protocol

from ..contracts import (
    ChangeDetection,
    ChangeStatus,
    ConfigurationChangeKind,
    ConfigurationEvent,
    EndpointConfigRevision,
    EndpointScope,
    EstimateStatus,
    IdentifiabilityStatus,
    MeasurementEndpoint,
    PhysicalDevice,
    ScopeStatus,
    utcnow,
)

log = logging.getLogger(__name__)


def confirmed_endpoint_estimate_status(
    endpoint: MeasurementEndpoint,
    subject_id: str,
) -> tuple[EstimateStatus, IdentifiabilityStatus] | None:
    """The estimate status for a subject backed by a single-subject endpoint.

    ``MEASURED`` is produced **only** through a confirmed single-subject scope
    (DEC-05): the endpoint must be enabled, have ``scope == SINGLE_SUBJECT``,
    ``scope_status == CONFIRMED`` and point at exactly ``subject_id``.  A
    candidate endpoint returns ``None`` (no measurement may be ingested yet).
    """
    if not endpoint.enabled:
        return None
    if endpoint.scope != EndpointScope.SINGLE_SUBJECT:
        return None
    if endpoint.scope_status != ScopeStatus.CONFIRMED:
        return None
    if endpoint.scope_subject_id != subject_id:
        return None
    return EstimateStatus.MEASURED, IdentifiabilityStatus.IDENTIFIABLE


def _revision_from_row(row: dict[str, Any]) -> EndpointConfigRevision:
    """Build an EndpointConfigRevision from a repository row dict."""
    return EndpointConfigRevision(
        endpoint_id=row["endpoint_id"],
        revision=int(row["revision"]),
        valid_from=_as_datetime(row["valid_from"]),
        valid_until=_as_datetime(row["valid_until"]) if row.get("valid_until") else None,
        phase=row.get("phase") or "unknown",
        leg=row.get("leg"),
        voltage_reference_v=row.get("voltage_reference_v"),
        polarity=row.get("polarity") or "normal",
        multiplier=float(row.get("multiplier") if row.get("multiplier") is not None else 1.0),
        calibration_version=row.get("calibration_version") or "unknown",
        measurement_type=row.get("measurement_type") or "instantaneous",
        measurement_window_ms=row.get("measurement_window_ms"),
        expected_interval_s=row.get("expected_interval_s"),
        resolution_w=row.get("resolution_w"),
        declared_by=row.get("declared_by") or "migration",
    )


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class EndpointRows(Protocol):
    """The S13 read surface the endpoint builders need (the ``entities`` repo)."""

    def endpoints(self, *, enabled_only: bool = False) -> list[dict[str, Any]]: ...

    def revisions(self, endpoint_id: str) -> list[dict[str, Any]]: ...


def endpoint_from_row(
    row: dict[str, Any], revision_rows: Iterable[dict[str, Any]] = ()
) -> MeasurementEndpoint:
    """Build the :class:`MeasurementEndpoint` contract from a raw S13 row.

    The repository row carries storage-only columns (``metadata_json``) that the
    contract forbids, so the contract is built field by field instead of with
    ``MeasurementEndpoint(**row)``.  The revisions are passed in rather than
    defaulted away: a confirmed single-subject CT validates only with its
    user-declared latest revision (DEC-05).  Raises on an invalid row.
    """
    return MeasurementEndpoint(
        endpoint_id=row["endpoint_id"],
        kind=row.get("kind") or "ct",
        entity_id=row.get("entity_id"),
        energy_entity_id=row.get("energy_entity_id"),
        display_name=row.get("display_name"),
        scope=row.get("scope") or "unknown",
        scope_status=row.get("scope_status") or "candidate",
        scope_subject_id=row.get("scope_subject_id"),
        panel_id=row.get("panel_id"),
        enabled=bool(row.get("enabled", True)),
        created_at=_as_datetime(row["created_at"]),
        revisions=[_revision_from_row(r) for r in revision_rows],
    )


def load_endpoint(entities: EndpointRows, row: dict[str, Any]) -> MeasurementEndpoint:
    """One endpoint contract, with its revisions loaded from ``entities``."""
    try:
        revisions = entities.revisions(row["endpoint_id"])
    except Exception:  # noqa: BLE001 - no revision source -> none
        revisions = []
    return endpoint_from_row(row, revisions)


def endpoints_from_repo(
    entities: EndpointRows, *, enabled_only: bool = False
) -> list[MeasurementEndpoint]:
    """Every valid S13 endpoint as a contract; an invalid row is skipped.

    This is the one place raw ``entities.endpoints()`` rows become contracts.
    The endpoints API, the topology v3 read and the matrix column tree all
    share it.  A malformed row is logged and skipped instead of failing the
    whole read, which would otherwise turn ``/topology/v3`` into a 400.
    """
    out: list[MeasurementEndpoint] = []
    for row in entities.endpoints(enabled_only=enabled_only):
        try:
            out.append(load_endpoint(entities, row))
        except Exception as exc:  # noqa: BLE001 - one bad row must not break the read
            log.warning(
                "skipping invalid measurement endpoint %r: %s", row.get("endpoint_id"), exc
            )
    return out


class DeviceService:
    """Create/retire devices and manage endpoint scope lifecycles."""

    def __init__(self, db: Any, *, clock: Any = None) -> None:
        self.db = db
        # Pure where possible: an injected clock keeps tests deterministic.
        self._clock = clock if clock is not None else utcnow

    # -- Physical Devices -------------------------------------------------- #

    def get(self, device_id: str) -> PhysicalDevice | None:
        row = self.db.repos.entities.get_device(device_id)
        if not row:
            return None
        return PhysicalDevice(
            device_id=row["device_id"],
            name=row.get("name") or device_id,
            device_type=row.get("device_type"),
            load_shape=row.get("load_shape"),
            sign_class=row.get("sign_class") or "consumer",
            ha_device_id=row.get("ha_device_id"),
            area_id=row.get("area_id"),
            origin=row.get("origin") or "migration",
            created_at=_as_datetime(row["created_at"]),
            retired_at=_as_datetime(row["retired_at"]) if row.get("retired_at") else None,
        )

    def create_device(self, device: PhysicalDevice) -> PhysicalDevice:
        """Create one Physical Device with an opaque id (never a ``dev_<ct>``)."""
        self.db.repos.entities.upsert_device(device)
        return device

    def retire(self, device_id: str, *, at: datetime | None = None) -> PhysicalDevice | None:
        """Retire a device without deleting it (legacy rows keep id + HA id)."""
        device = self.get(device_id)
        if device is None:
            return None
        stamp = at or self._clock()
        self.db.repos.entities.retire_device(device_id, stamp)
        return device.model_copy(update={"retired_at": stamp})

    # -- Measurement Endpoints -------------------------------------------- #

    def _endpoint(self, row: dict[str, Any]) -> MeasurementEndpoint:
        return load_endpoint(self.db.repos.entities, row)

    def get_endpoint(self, endpoint_id: str) -> MeasurementEndpoint | None:
        row = self.db.repos.entities.get_endpoint(endpoint_id)
        return self._endpoint(row) if row else None

    def list_endpoints(self) -> list[MeasurementEndpoint]:
        return endpoints_from_repo(self.db.repos.entities)

    def upsert_endpoint(self, endpoint: MeasurementEndpoint) -> MeasurementEndpoint:
        """Persist a discovered endpoint and its initial config revision."""
        self.db.repos.entities.upsert_endpoint(endpoint)
        if not self.db.repos.entities.revisions(endpoint.endpoint_id):
            self.db.repos.entities.append_endpoint_revision(
                EndpointConfigRevision(
                    endpoint_id=endpoint.endpoint_id,
                    revision=1,
                    valid_from=endpoint.created_at,
                    phase="unknown",
                    polarity="normal",
                    multiplier=1.0,
                    calibration_version="discovery",
                    measurement_type="instantaneous",
                    declared_by="discovery",
                )
            )
        return self.get_endpoint(endpoint.endpoint_id)

    def _append_revision(self, endpoint: MeasurementEndpoint, *, declared_by: str = "user") -> None:
        revisions = self.db.repos.entities.revisions(endpoint.endpoint_id)
        next_revision = (max((int(r["revision"]) for r in revisions), default=0) or 0) + 1
        self.db.repos.entities.append_endpoint_revision(
            EndpointConfigRevision(
                endpoint_id=endpoint.endpoint_id,
                revision=next_revision,
                valid_from=self._clock(),
                phase="unknown",
                polarity="normal",
                multiplier=1.0,
                calibration_version="scope-change",
                measurement_type="instantaneous",
                declared_by=declared_by,
            )
        )

    def _record_event(
        self,
        endpoint: MeasurementEndpoint,
        *,
        action: str,
    ) -> dict[str, Any]:
        """Append an immutable ConfigurationEvent for a scope/name change."""
        event = ConfigurationEvent(
            event_id=f"cfg::{endpoint.endpoint_id}::{action}::{self._clock().isoformat()}",
            kind=ConfigurationChangeKind.MEASUREMENT_SCOPE_CHANGE,
            detection=ChangeDetection.DECLARED,
            status=ChangeStatus.CONFIRMED,
            affected_endpoint_ids=[endpoint.endpoint_id],
            created_at=self._clock(),
        )
        self.db.repos.topology_v3.append_configuration_event(event)
        return event.model_dump(mode="json")

    def set_scope(
        self,
        endpoint_id: str,
        action: str,
        subject_id: str | None = None,
    ) -> dict[str, Any]:
        """Confirm or reject an endpoint's scope, appending revision + event.

        ``confirm`` sets ``SINGLE_SUBJECT``/CONFIRMED and enables ingestion;
        ``reject`` sets REJECTED and disables it.  Both are append-only.
        """
        endpoint = self.get_endpoint(endpoint_id)
        if endpoint is None:
            raise KeyError(f"no endpoint {endpoint_id!r}")
        if action == "confirm":
            subject = subject_id or endpoint.scope_subject_id
            if subject is None:
                raise ValueError("subject_id is required to confirm this endpoint")
            updated = endpoint.model_copy(
                update={
                    "scope": EndpointScope.SINGLE_SUBJECT,
                    "scope_status": ScopeStatus.CONFIRMED,
                    "scope_subject_id": subject,
                    "enabled": True,
                }
            )
        elif action == "reject":
            updated = endpoint.model_copy(
                update={"scope_status": ScopeStatus.REJECTED, "enabled": False}
            )
        else:
            raise ValueError(f"unknown scope action {action!r}")
        # Append the user-declared revision *before* the upsert: for a CT whose
        # scope is being confirmed as SINGLE_SUBJECT, the endpoint validator
        # requires a user-declared latest revision to exist (DEC-05).
        self._append_revision(updated)
        self.db.repos.entities.upsert_endpoint(updated)
        event = self._record_event(updated, action=f"scope.{action}")
        fresh = self.get_endpoint(endpoint_id)
        return {"endpoint": fresh, "configuration_events": [event]}

    def update_endpoint(
        self,
        endpoint_id: str,
        *,
        display_name: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Update display name / enabled, appending a config event."""
        endpoint = self.get_endpoint(endpoint_id)
        if endpoint is None:
            raise KeyError(f"no endpoint {endpoint_id!r}")
        updates: dict[str, Any] = {}
        if display_name is not None:
            updates["display_name"] = display_name
        if enabled is not None:
            updates["enabled"] = bool(enabled)
        updated = endpoint.model_copy(update=updates)
        self.db.repos.entities.upsert_endpoint(updated)
        event = self._record_event(updated, action="update")
        return {"endpoint": self.get_endpoint(endpoint_id), "configuration_events": [event]}

    # -- Facade adapter ---------------------------------------------------- #

    def as_facade(self) -> EndpointFacade:
        """A thin facade exposing the endpoints HTTP surface."""
        return EndpointFacade(self)


class EndpointFacade:
    """The facade methods backing ``api/routes/endpoints.py``."""

    def __init__(self, service: DeviceService) -> None:
        self.service = service

    def endpoints(self) -> dict[str, Any]:
        return {"endpoints": [e.model_dump(mode="json") for e in self.service.list_endpoints()]}

    def endpoint(self, endpoint_id: str) -> dict[str, Any] | None:
        endpoint = self.service.get_endpoint(endpoint_id)
        return endpoint.model_dump(mode="json") if endpoint is not None else None

    def set_endpoint_scope(
        self, endpoint_id: str, action: str, subject_id: str | None = None
    ) -> dict[str, Any]:
        result = self.service.set_scope(endpoint_id, action, subject_id)
        return {
            "endpoint": result["endpoint"].model_dump(mode="json"),
            "configuration_events": result["configuration_events"],
        }

    def update_endpoint(
        self, endpoint_id: str, *, display_name: str | None = None, enabled: bool | None = None
    ) -> dict[str, Any]:
        result = self.service.update_endpoint(
            endpoint_id, display_name=display_name, enabled=enabled
        )
        return {
            "endpoint": result["endpoint"].model_dump(mode="json"),
            "configuration_events": result["configuration_events"],
        }


__all__ = [
    "DeviceService",
    "EndpointFacade",
    "EndpointRows",
    "confirmed_endpoint_estimate_status",
    "endpoint_from_row",
    "endpoints_from_repo",
    "load_endpoint",
]
