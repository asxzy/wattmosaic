"""Repository for the canonical entity tables created by migration 13.

Reached as ``db.repos.entities`` (the lazy gateway in
:mod:`storage.repositories` resolves a module's single ``Repository`` subclass).
All methods return plain dicts keyed by column name: callers instantiate the
:mod:`wattmosaic_engine.contracts` models themselves, which keeps this module
free of validation of legacy data (``extra="forbid"`` is a contract concern, not
a storage one).

Bulk reads use ``_reader()`` -- the short-lived, ``query_only`` connection -- so
a large entity scan never blocks ``/health``.  Writes take ``self._lock`` and
run in ``BEGIN IMMEDIATE``, matching the other repositories.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..repositories import Repository, as_utc, iso

# Columns returned by each table's read methods.  Spelt out rather than
# ``SELECT *`` so the dict shape is stable if the migration adds a column later.
_ENDPOINT_COLUMNS = (
    "endpoint_id", "kind", "entity_id", "energy_entity_id", "display_name",
    "scope", "scope_status", "scope_subject_id", "panel_id", "enabled",
    "created_at", "metadata_json",
)
_REVISION_COLUMNS = (
    "endpoint_id", "revision", "valid_from", "valid_until", "phase", "leg",
    "voltage_reference_v", "polarity", "multiplier", "calibration_version",
    "measurement_type", "measurement_window_ms", "expected_interval_s",
    "resolution_w", "declared_by",
)
_DEVICE_COLUMNS = (
    "device_id", "name", "device_type", "load_shape", "sign_class",
    "ha_device_id", "area_id", "origin", "created_at", "retired_at", "metadata_json",
)
_COMPONENT_COLUMNS = (
    "component_id", "device_id", "name", "load_shape", "sign_class",
)
_ENTITY_COLUMNS = (
    "entity_id", "domain", "ha_device_id", "first_seen", "last_seen",
)
_LINK_COLUMNS = (
    "entity_id", "target_kind", "target_id", "role", "valid_from", "valid_until",
    "declared_by",
)


def _row(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


class EntitiesRepository(Repository):
    """CRUD over the migration-13 entity tables."""

    # -- measurement endpoints -------------------------------------------- #

    def get_endpoint(self, endpoint_id: str) -> dict[str, Any] | None:
        with self._reader() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_ENDPOINT_COLUMNS)} FROM measurement_endpoints "
                "WHERE endpoint_id = ?",
                (endpoint_id,),
            ).fetchone()
        return _row(row) if row is not None else None

    def endpoints(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = f"SELECT {', '.join(_ENDPOINT_COLUMNS)} FROM measurement_endpoints"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY endpoint_id"
        with self._reader() as conn:
            return [_row(row) for row in conn.execute(query).fetchall()]

    def upsert_endpoint(self, endpoint: Any) -> None:
        """Insert or update one endpoint from a contract model or a plain dict.

        The full row is replaced (``INSERT OR REPLACE``) on an explicit write;
        append-only revisions are handled separately by
        :meth:`append_endpoint_revision` so a config edit never loses history.
        """
        data = _model_dict(endpoint)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO measurement_endpoints(
                        endpoint_id, kind, entity_id, energy_entity_id, display_name, scope,
                        scope_status, scope_subject_id, panel_id, enabled, created_at, metadata_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        data["endpoint_id"], _enum(data.get("kind"), "ct"),
                        data.get("entity_id"), data.get("energy_entity_id"),
                        data.get("display_name"), _enum(data.get("scope"), "unknown"),
                        _enum(data.get("scope_status"), "candidate"),
                        data.get("scope_subject_id"), data.get("panel_id"),
                        int(bool(data.get("enabled", True))), iso(_coerce_time(data.get("created_at"))),
                        _json(data.get("metadata", data.get("metadata_json", {}))),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def delete_endpoint(self, endpoint_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM measurement_endpoints WHERE endpoint_id = ?", (endpoint_id,))

    # -- endpoint configuration revisions (append-only) ------------------- #

    def revisions(self, endpoint_id: str) -> list[dict[str, Any]]:
        with self._reader() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_REVISION_COLUMNS)} FROM endpoint_config_revisions "
                "WHERE endpoint_id = ? ORDER BY revision",
                (endpoint_id,),
            ).fetchall()
        return [_row(row) for row in rows]

    def append_endpoint_revision(self, revision: Any) -> None:
        """Append one immutable revision; refuse a duplicate ``(id, revision)``."""
        data = _model_dict(revision)
        valid_from = iso(_coerce_time(data["valid_from"]))
        valid_until = data.get("valid_until")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT INTO endpoint_config_revisions(
                        endpoint_id, revision, valid_from, valid_until, phase, leg,
                        voltage_reference_v, polarity, multiplier, calibration_version,
                        measurement_type, measurement_window_ms, expected_interval_s,
                        resolution_w, declared_by)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        data["endpoint_id"], int(data["revision"]), valid_from,
                        iso(_coerce_time(valid_until)) if valid_until is not None else None,
                        _enum(data.get("phase"), "unknown"), data.get("leg"),
                        data.get("voltage_reference_v"),
                        _enum(data.get("polarity"), "normal"),
                        float(data["multiplier"]), data["calibration_version"],
                        _enum(data.get("measurement_type"), "instantaneous"),
                        data.get("measurement_window_ms"), data.get("expected_interval_s"),
                        data.get("resolution_w"), _enum(data.get("declared_by"), "migration"),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def endpoint_revision_at(self, endpoint_id: str, at: datetime | str) -> dict[str, Any] | None:
        """The revision in force at instant ``at`` (the one with the greatest
        ``valid_from`` not after ``at`` that has not yet ended), or ``None`` when
        no revision covers that instant.

        The time-scoped multiplier history is why an old sample must not be
        re-normalised with today's calibration (DEF-06): callers pass the
        sample's ``observed_at`` and get the factor that was true then.
        """
        stamp = iso(_coerce_time(at))
        with self._reader() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_REVISION_COLUMNS)} FROM endpoint_config_revisions "
                "WHERE endpoint_id = ? AND valid_from <= ? "
                "AND (valid_until IS NULL OR valid_until > ?) "
                "ORDER BY valid_from DESC, revision DESC LIMIT 1",
                (endpoint_id, stamp, stamp),
            ).fetchone()
        return _row(row) if row is not None else None

    # -- physical devices -------------------------------------------------- #

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self._reader() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_DEVICE_COLUMNS)} FROM physical_devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return _row(row) if row is not None else None

    def devices(self, *, origin: str | None = None) -> list[dict[str, Any]]:
        query = f"SELECT {', '.join(_DEVICE_COLUMNS)} FROM physical_devices"
        params: tuple[Any, ...] = ()
        if origin is not None:
            query += " WHERE origin = ?"
            params = (origin,)
        query += " ORDER BY device_id"
        with self._reader() as conn:
            return [_row(row) for row in conn.execute(query, params).fetchall()]

    def upsert_device(self, device: Any) -> None:
        data = _model_dict(device)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO physical_devices(
                        device_id, name, device_type, load_shape, sign_class, ha_device_id,
                        area_id, origin, created_at, retired_at, metadata_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        data["device_id"], data["name"], data.get("device_type"),
                        _enum_or_none(data.get("load_shape")),
                        _enum(data.get("sign_class"), "consumer"),
                        data.get("ha_device_id"), data.get("area_id"),
                        _enum(data.get("origin"), "migration"),
                        iso(_coerce_time(data["created_at"])),
                        iso(_coerce_time(data.get("retired_at"))) if data.get("retired_at") else None,
                        _json(data.get("metadata", data.get("metadata_json", {}))),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def delete_device(self, device_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM physical_devices WHERE device_id = ?", (device_id,))

    def retire_device(self, device_id: str, at: datetime | str) -> None:
        """Mark a device retired without deleting it (D2's retirement workflow)."""
        with self._lock:
            self._conn.execute(
                "UPDATE physical_devices SET retired_at = ? WHERE device_id = ?",
                (iso(_coerce_time(at)), device_id),
            )

    # -- load components --------------------------------------------------- #

    def components(self, device_id: str | None = None) -> list[dict[str, Any]]:
        query = f"SELECT {', '.join(_COMPONENT_COLUMNS)} FROM load_components"
        params: tuple[Any, ...] = ()
        if device_id is not None:
            query += " WHERE device_id = ?"
            params = (device_id,)
        query += " ORDER BY component_id"
        with self._reader() as conn:
            return [_row(row) for row in conn.execute(query, params).fetchall()]

    def upsert_component(self, component: Any) -> None:
        data = _model_dict(component)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO load_components(
                        component_id, device_id, name, load_shape, sign_class)
                    VALUES(?,?,?,?,?)""",
                    (
                        data["component_id"], data["device_id"], data["name"],
                        _enum_or_none(data.get("load_shape")),
                        _enum(data.get("sign_class"), "consumer"),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # -- HA entities and their links -------------------------------------- #

    def get_ha_entity(self, entity_id: str) -> dict[str, Any] | None:
        with self._reader() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_ENTITY_COLUMNS)} FROM ha_entities WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
        return _row(row) if row is not None else None

    def ha_entities(self) -> list[dict[str, Any]]:
        with self._reader() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_ENTITY_COLUMNS)} FROM ha_entities ORDER BY entity_id"
            ).fetchall()
        return [_row(row) for row in rows]

    def upsert_ha_entity(self, entity: Any) -> None:
        data = _model_dict(entity)
        first = iso(_coerce_time(data["first_seen"]))
        last = iso(_coerce_time(data["last_seen"]))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT OR REPLACE INTO ha_entities(
                        entity_id, domain, ha_device_id, first_seen, last_seen)
                    VALUES(?,?,?,?,?)""",
                    (data["entity_id"], data["domain"], data.get("ha_device_id"), first, last),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def links_for_entity(
        self, entity_id: str, at: datetime | str | None = None
    ) -> list[dict[str, Any]]:
        """Every subject this HA entity is linked to, optionally as of ``at``.

        A many-to-many entity may link to several devices/components (DEC-08);
        the result is ordered by target so a caller can use it directly.  When
        ``at`` is given, links outside their validity interval are excluded.
        """
        query = f"SELECT {', '.join(_LINK_COLUMNS)} FROM ha_entity_links WHERE entity_id = ?"
        params: list[Any] = [entity_id]
        if at is not None:
            stamp = iso(_coerce_time(at))
            query += " AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)"
            params += [stamp, stamp]
        query += " ORDER BY target_kind, target_id, valid_from"
        with self._reader() as conn:
            return [_row(row) for row in conn.execute(query, params).fetchall()]

    def links(self) -> list[dict[str, Any]]:
        """Every stored HA entity link, ordered by entity then target."""
        query = (
            f"SELECT {', '.join(_LINK_COLUMNS)} FROM ha_entity_links "
            "ORDER BY entity_id, target_kind, target_id, valid_from"
        )
        with self._reader() as conn:
            return [_row(row) for row in conn.execute(query).fetchall()]

    def subjects_for_ha_device(self, ha_device_id: str) -> list[dict[str, Any]]:
        """All ``(entity, target_kind, target_id, role)`` links whose HA entity
        belongs to a given Home Assistant device id.

        Used by device-link discovery to expand an HA device into the load
        subjects its entities describe, without consulting HA at query time.
        """
        query = (
            f"SELECT {', '.join(_LINK_COLUMNS)} "
            "FROM ha_entity_links AS l JOIN ha_entities AS e USING (entity_id) "
            "WHERE e.ha_device_id = ? "
            "ORDER BY l.entity_id, l.target_kind, l.target_id, l.valid_from"
        )
        with self._reader() as conn:
            return [_row(row) for row in conn.execute(query, (ha_device_id,)).fetchall()]

    def add_entity_link(self, link: Any) -> None:
        """Add one HA entity link (idempotent by its primary key).

        The linked :class:`HaEntity` row is created first when it is not known
        yet (``ha_entity_links.entity_id`` references ``ha_entities``), seen at
        the link's ``valid_from``; an existing entity row is left unchanged.
        """
        data = _model_dict(link)
        entity_id = str(data["entity_id"])
        seen = iso(_coerce_time(data["valid_from"]))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """INSERT OR IGNORE INTO ha_entities(
                        entity_id, domain, ha_device_id, first_seen, last_seen)
                    VALUES(?,?,?,?,?)""",
                    (entity_id, entity_id.split(".", 1)[0], None, seen, seen),
                )
                self._conn.execute(
                    """INSERT OR IGNORE INTO ha_entity_links(
                        entity_id, target_kind, target_id, role, valid_from, valid_until, declared_by)
                    VALUES(?,?,?,?,?,?,?)""",
                    (
                        data["entity_id"], _enum(data.get("target_kind"), "device"),
                        data["target_id"], _enum(data.get("role"), "unknown"),
                        iso(_coerce_time(data["valid_from"])),
                        iso(_coerce_time(data["valid_until"])) if data.get("valid_until") else None,
                        _enum(data.get("declared_by"), "migration"),
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def delete_entity_link(
        self, entity_id: str, target_kind: str, target_id: str, valid_from: datetime | str
    ) -> None:
        with self._lock:
            self._conn.execute(
                """DELETE FROM ha_entity_links
                WHERE entity_id = ? AND target_kind = ? AND target_id = ? AND valid_from = ?""",
                (entity_id, target_kind, target_id, iso(_coerce_time(valid_from))),
            )


# --------------------------------------------------------------------------- #
# Model/dict coercion helpers
# --------------------------------------------------------------------------- #


def _model_dict(value: Any) -> dict[str, Any]:
    """A plain dict from a pydantic model or a mapping."""
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="python"))
    return dict(vars(value))


def _enum(value: Any, default: str) -> str:
    return str(getattr(value, "value", value) if value is not None else default)


def _enum_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _coerce_time(value: Any) -> datetime:
    """Accept a datetime, an ISO string, or fall back to the Unix epoch."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return as_utc(value)
    return datetime.fromtimestamp(0, tz=UTC)  # pragma: no cover


def _json(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


__all__ = ["EntitiesRepository"]
