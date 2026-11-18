"""Power models: legacy tables plus the v3 append-only version registry.

The legacy part (``device_models``, ``device_model_versions``, ``device_flags``)
keeps its old activation pointer for Wave-0 callers.  Migration 18 (S18) adds the
v3 registry: ``power_model_versions_v3`` (append-only, keyed by subject rather
than ``(device, model_type)``), ``power_model_events`` (append-only status log)
and, in :mod:`.learning`, ``learning_observations``.  Legacy absolute-basis rows
are copied by the migration with ``basis=LEGACY_ABSOLUTE`` and a ``legacy`` spec;
DEC-31 forbids serving them as validated power or as priors, so no legacy copy
is ever active -- the event log is the only source of "active".

``active_version`` derives the active version from the event log (never from a
mutable pointer): the last ``activated`` without a later ``superseded`` or
``rolled_back``.  A never-activated or ``rejected`` version is never returned
(DEF-07), and a rollback reactivates the last previously activated eligible
version.  ``append_version`` / ``append_event`` are plain INSERTs; no
``INSERT OR REPLACE`` rewrites a version's history.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Literal

from pydantic import Field, TypeAdapter

from ...contracts import (
    AdjustableSpec,
    ComponentSumSpec,
    DeviceModelVersion,
    DynamicSpec,
    EvidenceRef,
    FixedSpec,
    ModelStatus,
    PowerModelEvent,
    PowerModelVersion,
    ResponseKernelSpec,
    utcnow,
)
from ...contracts.legacy import Model as ContractModel
from ..repositories import Repository, enum_value, iso


class ModelsRepository(Repository):
    def save_model_version(self, model: DeviceModelVersion, activate: bool = False) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO device_model_versions(device_id, model_type, version, status,
                    created_at, trained_from, trained_to, sample_count, training_quality,
                    validation_metrics_json, parameters_json, confidence, notes_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        model.device_id, model.model_type, model.version, model.status, iso(model.created_at),
                        iso(model.trained_from), iso(model.trained_to), model.sample_count,
                        model.training_quality, json.dumps(model.validation_metrics),
                        json.dumps(model.parameters.model_dump(), default=str), model.confidence,
                        json.dumps(model.notes),
                    ),
                )
                model_id = f"{model.device_id}:{model.model_type}"
                active = model.version if activate else None
                self._conn.execute(
                    """INSERT INTO device_models(model_id, device_id, model_type, active_version, frozen,
                    created_at, updated_at) VALUES(?,?,?,?,0,?,?)
                    ON CONFLICT(model_id, device_id) DO UPDATE SET
                    active_version=COALESCE(?, active_version), updated_at=excluded.updated_at""",
                    (model_id, model.device_id, model.model_type, active, iso(utcnow()), iso(utcnow()), active),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def active_model(self, device_id: str, model_type: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if model_type:
                row = self._conn.execute(
                    """SELECT v.* FROM device_model_versions v JOIN device_models m
                    ON v.device_id=m.device_id AND v.model_type=m.model_type AND v.version=m.active_version
                    WHERE v.device_id=? AND v.model_type=?""",
                    (device_id, model_type),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """SELECT v.* FROM device_model_versions v JOIN device_models m
                    ON v.device_id=m.device_id AND v.model_type=m.model_type AND v.version=m.active_version
                    WHERE v.device_id=? ORDER BY v.created_at DESC LIMIT 1""",
                    (device_id,),
                ).fetchone()
        return dict(row) if row else None

    def model_versions(self, device_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM device_model_versions WHERE device_id=? ORDER BY version DESC", (device_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def set_model_status(self, device_id: str, model_type: str, version: int, status: ModelStatus) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE device_model_versions SET status=? WHERE device_id=? AND model_type=? AND version=?",
                (enum_value(status), device_id, model_type, version),
            )

    def set_model_frozen(self, device_id: str, frozen: bool) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO device_flags(device_id, frozen, updated_at) VALUES(?,?,?)
                ON CONFLICT(device_id) DO UPDATE SET frozen=excluded.frozen, updated_at=excluded.updated_at""",
                (device_id, int(frozen), iso(utcnow())),
            )
            self._conn.execute(
                "UPDATE device_models SET frozen=?, updated_at=? WHERE device_id=?",
                (int(frozen), iso(utcnow()), device_id),
            )

    def is_model_frozen(self, device_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT frozen FROM device_flags WHERE device_id=?", (device_id,)
            ).fetchone()
        return bool(row and row["frozen"])

    def rollback_model(self, device_id: str, model_type: str) -> int | None:
        with self._lock:
            rows = self._conn.execute(
                """SELECT version FROM device_model_versions WHERE device_id=? AND model_type=?
                ORDER BY version DESC LIMIT 2""",
                (device_id, model_type),
            ).fetchall()
            if len(rows) < 2:
                return None
            previous = int(rows[1]["version"])
            self._conn.execute(
                "UPDATE device_model_versions SET status='rolled_back' WHERE device_id=? AND model_type=? AND version=?",
                (device_id, model_type, rows[0]["version"]),
            )
            self._conn.execute(
                """UPDATE device_models SET active_version=?, updated_at=? WHERE device_id=? AND model_type=?""",
                (previous, iso(utcnow()), device_id, model_type),
            )
            return previous

    # -- v3 append-only registry (S18) ----------------------------------- #
    def append_version(self, version: PowerModelVersion) -> None:
        """Append one version row.  A duplicate primary key is an error, not an
        update: a version's spec is immutable once written.
        """
        with self._lock:
            self._conn.execute(
                """INSERT INTO power_model_versions_v3(
                    version_id, subject_kind, subject_id, number, model_class, basis,
                    spec_json, schema_version, evidence_refs_json, topology_version_at_training,
                    trainer_version, parent_version_id, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version.version_id,
                    version.subject_kind,
                    version.subject_id,
                    version.number,
                    enum_value(version.model_class),
                    enum_value(version.basis),
                    json.dumps(version.spec.model_dump(mode="json"), sort_keys=True),
                    version.schema_version,
                    json.dumps(
                        [ref.model_dump(mode="json") for ref in version.evidence_refs],
                        sort_keys=True,
                    ),
                    version.topology_version_at_training,
                    version.trainer_version,
                    version.parent_version_id,
                    iso(version.created_at),
                ),
            )

    def append_event(self, event: PowerModelEvent) -> None:
        """Append one lifecycle event (the only source of "active" truth)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO power_model_events(version_id, event, reason, at) VALUES(?,?,?,?)",
                (event.version_id, event.event, event.reason, iso(event.at)),
            )

    @staticmethod
    def _decode_spec(raw: str, model_class: str) -> Any:
        """Decode a stored spec by class, with a read-only ``LegacySpec`` fallback.

        Legacy copies carry ``{"kind": "legacy", ...}``, which is not a member of
        the K06 :data:`PowerModelSpec` union.  The variant exists only here, on
        the read path, and is deliberately not exported from contracts (DEC-31).
        """
        payload = json.loads(raw) if raw else {}
        if isinstance(payload, dict) and payload.get("kind") == "legacy":
            return LegacySpec(**payload)

        adapters: dict[str, TypeAdapter[Any]] = {
            "FIXED": TypeAdapter(FixedSpec),
            "ADJUSTABLE": TypeAdapter(AdjustableSpec),
            "DYNAMIC": TypeAdapter(DynamicSpec),
            "RESPONSE_KERNEL": TypeAdapter(ResponseKernelSpec),
            "COMPONENT_SUM": TypeAdapter(ComponentSumSpec),
        }
        adapter = adapters.get(model_class)
        if adapter is None:
            return payload
        try:
            return adapter.validate_python(payload)
        except Exception:
            return payload

    @staticmethod
    def _decode_version(row: Any) -> dict[str, Any]:
        out = dict(row)
        out["spec"] = ModelsRepository._decode_spec(
            out.pop("spec_json", "{}"), out.get("model_class", "")
        )
        out["evidence_refs"] = [
            EvidenceRef.model_validate(ref)
            for ref in json.loads(out.pop("evidence_refs_json", "[]") or "[]")
        ]
        return out

    def _version_row(self, version_id: str) -> Any:
        return self._conn.execute(
            "SELECT * FROM power_model_versions_v3 WHERE version_id=?", (version_id,)
        ).fetchone()

    @staticmethod
    def _latest_event(rows: list[Any]) -> Any | None:
        """Last event by (at, id); ties break on insertion order (append-only)."""
        if not rows:
            return None
        return max(rows, key=lambda row: (str(row["at"]), int(row["id"])))

    def active_version(self, subject_id: str) -> dict[str, Any] | None:
        """Derive the subject's active version from its event log.

        A version is active iff its last event is ``activated``.  That makes a
        never-activated or ``rejected`` version ineligible (DEF-07), and a
        ``rolled_back`` version reverts to the last version whose log shows
        ``activated`` (itself the one the rollback event names).  Only one row
        per subject can satisfy this invariant.
        """
        with self._lock:
            events = self._conn.execute(
                """SELECT e.* FROM power_model_events e
                   JOIN power_model_versions_v3 v ON v.version_id=e.version_id
                   WHERE v.subject_id=? ORDER BY e.at ASC, e.id ASC""",
                (subject_id,),
            ).fetchall()
            by_version: dict[str, list[Any]] = defaultdict(list)
            for event in events:
                by_version[event["version_id"]].append(event)
            eligible = [
                version_id
                for version_id, rows in by_version.items()
                if self._latest_event(rows)["event"] == "activated"
            ]
            if not eligible:
                return None
            # The latest activation wins; a rollback appended a `rolled_back`
            # event on the old version, so at most one remains eligible.
            winner = max(
                eligible,
                key=lambda version_id: (
                    str(self._latest_event(by_version[version_id])["at"]),
                    int(self._latest_event(by_version[version_id])["id"]),
                ),
            )
            row = self._version_row(winner)
            return self._decode_version(row) if row is not None else None

    def versions(self, subject_id: str) -> list[dict[str, Any]]:
        """Every version of a subject, oldest number first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM power_model_versions_v3 WHERE subject_id=? "
                "ORDER BY subject_kind, number, version_id",
                (subject_id,),
            ).fetchall()
        return [self._decode_version(row) for row in rows]


# A read-only view of a legacy ``{"kind": "legacy", ...}`` spec.  It is defined
# here (not in contracts) because DEC-31 keeps legacy models out of the v3
# union: a legacy model is copied for provenance, never served as validated
# power or used as a prior.
class LegacySpec(ContractModel):
    kind: Literal["legacy"] = "legacy"
    parameters: dict[str, Any] = Field(default_factory=dict)


__all__ = ["LegacySpec", "ModelsRepository"]
