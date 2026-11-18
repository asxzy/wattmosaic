"""Migration v013 — canonical entity tables and lossless backfill.

The four HB §1.1 object kinds become first-class tables, with ids preserved:

* ``ct_channels.ct_id`` -> ``measurement_endpoints.endpoint_id`` (plus an
  append-only configuration history in ``endpoint_config_revisions``);
* ``devices.device_id`` -> ``physical_devices.device_id`` verbatim (DEC-04);
* HA entity evidence, which today lives in four places, is consolidated into
  ``ha_entities`` + the many-to-many ``ha_entity_links`` (DEC-08).

``load_components`` is created empty: components are user-declared only (DEC-07)
and there is no legacy representation to import.

Design constraints honoured here:

* **Idempotent.** Every DDL statement uses ``IF NOT EXISTS`` and every backfill
  insert uses ``INSERT OR IGNORE``, so running ``apply`` twice is a true no-op.
* **Foreign keys.** ``PRAGMA foreign_keys=ON`` is set on the production
  connection, so rows are inserted parent-first: endpoints before revisions,
  devices before components, HA entities before links.
* **Tolerant legacy reads.** ``device_configurations.payload_json`` is parsed
  with :func:`decode_payload` and read through plain dict access.  A payload
  carrying a retired role (``monitoring == "dedicated"``) is *not* pushed through
  a Pydantic model with ``extra="forbid"`` -- it is read as data and flagged.
* **No legacy mutation.** Nothing here drops or alters a legacy table; Z1 (v25)
  is the only task that renames them (DEC-02).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from ..payload import decode_payload
from . import Migration

#: Stable fallback instant when a source row carries no usable timestamp.
#: Using a constant (rather than ``utcnow()``) keeps the migration deterministic
#: and keeps a second application from producing a different ``valid_from``.
_FALLBACK_TIME = "2026-01-01T00:00:00+00:00"
#: Start of the open-ended "before any recorded factor" baseline revision.  A
#: legacy CT carries no creation timestamp (``updated_at`` is the last topology
#: write, not when the channel was installed), so a fixed early instant is the
#: honest choice -- it makes the baseline cover every historical sample until
#: the first factor interval begins.
_BASELINE_START = "2000-01-01T00:00:00+00:00"
_REVISION_LABEL = "legacy"  # calibration_version of the backfilled baseline revision

#: Storage tier of every table created here (all versioned entity state).
TIERS: dict[str, str] = {
    "measurement_endpoints": "L4",
    "endpoint_config_revisions": "L4",
    "physical_devices": "L4",
    "load_components": "L4",
    "ha_entities": "L4",
    "ha_entity_links": "L4",
}

_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS measurement_endpoints (
        endpoint_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        entity_id TEXT,
        energy_entity_id TEXT,
        display_name TEXT,
        scope TEXT NOT NULL,
        scope_status TEXT NOT NULL,
        scope_subject_id TEXT,
        panel_id TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS endpoint_config_revisions (
        endpoint_id TEXT NOT NULL REFERENCES measurement_endpoints(endpoint_id),
        revision INTEGER NOT NULL,
        valid_from TEXT NOT NULL,
        valid_until TEXT,
        phase TEXT NOT NULL,
        leg TEXT,
        voltage_reference_v REAL,
        polarity TEXT NOT NULL,
        multiplier REAL NOT NULL,
        calibration_version TEXT NOT NULL,
        measurement_type TEXT NOT NULL,
        measurement_window_ms INTEGER,
        expected_interval_s REAL,
        resolution_w REAL,
        declared_by TEXT NOT NULL,
        PRIMARY KEY (endpoint_id, revision)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS physical_devices (
        device_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        device_type TEXT,
        load_shape TEXT,
        sign_class TEXT NOT NULL DEFAULT 'consumer',
        ha_device_id TEXT,
        area_id TEXT,
        origin TEXT NOT NULL,
        created_at TEXT NOT NULL,
        retired_at TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS load_components (
        component_id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL REFERENCES physical_devices(device_id),
        name TEXT NOT NULL,
        load_shape TEXT,
        sign_class TEXT NOT NULL DEFAULT 'consumer'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ha_entities (
        entity_id TEXT PRIMARY KEY,
        domain TEXT NOT NULL,
        ha_device_id TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ha_entity_links (
        entity_id TEXT NOT NULL REFERENCES ha_entities(entity_id),
        target_kind TEXT NOT NULL,
        target_id TEXT NOT NULL,
        role TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        valid_until TEXT,
        declared_by TEXT NOT NULL,
        PRIMARY KEY (entity_id, target_kind, target_id, valid_from)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ep_revisions_time ON endpoint_config_revisions(endpoint_id, valid_from)",
    "CREATE INDEX IF NOT EXISTS idx_ha_links_entity ON ha_entity_links(entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_ha_links_target ON ha_entity_links(target_kind, target_id)",
)

# --------------------------------------------------------------------------- #
# Tolerant helpers
# --------------------------------------------------------------------------- #


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_timestamp(value: Any) -> str:
    """Normalise a stored timestamp to an ISO string, with a stable fallback."""
    text = _as_text(value)
    if text is None:
        return _FALLBACK_TIME
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _FALLBACK_TIME
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _json_object(raw: Any) -> dict[str, Any]:
    """Parse a JSON cell into a dict; never raise on legacy/corrupt content."""
    parsed = decode_payload(raw) if isinstance(raw, str) else None
    return parsed if isinstance(parsed, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := _as_text(item))]


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else "unknown"


def _valid_role(value: Any) -> str:
    """Coerce a legacy role string to a valid :class:`HaEntityRole` value."""
    allowed = {
        "load_state",
        "mode_only",
        "power_measurement",
        "energy_counter",
        "availability",
        "unknown",
    }
    text = _as_text(value)
    return text if text in allowed else "unknown"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# Endpoint + configuration-revision backfill
# --------------------------------------------------------------------------- #


def _ct_factors(conn: sqlite3.Connection, ct_id: str) -> list[dict[str, Any]]:
    """Time-scoped multiplier history for one CT, ordered by start time."""
    if not _table_exists(conn, "ct_measurement_factors"):
        return []
    rows = conn.execute(
        "SELECT id, ct_id, multiplier, effective_from, effective_to, declared_by "
        "FROM ct_measurement_factors WHERE ct_id = ? ORDER BY effective_from, id",
        (ct_id,),
    ).fetchall()
    return [
        {
            "multiplier": row[2],
            "valid_from": _as_timestamp(row[3]),
            "valid_until": _as_timestamp(row[4]) if row[4] is not None else None,
            "declared_by": _as_text(row[5]) or "migration",
        }
        for row in rows
    ]


def _insert_revision(conn: sqlite3.Connection, endpoint_id: str, revision: int, *, valid_from: str,
                     valid_until: str | None, phase: str, polarity: str, multiplier: float,
                     expected_interval_s: float | None, resolution_w: float | None,
                     declared_by: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO endpoint_config_revisions(
            endpoint_id, revision, valid_from, valid_until, phase, leg, voltage_reference_v,
            polarity, multiplier, calibration_version, measurement_type, measurement_window_ms,
            expected_interval_s, resolution_w, declared_by)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            endpoint_id,
            revision,
            valid_from,
            valid_until,
            phase,
            None,  # leg -- not recorded on a legacy CT
            None,  # voltage_reference_v
            polarity,
            float(multiplier),
            _REVISION_LABEL,
            "instantaneous",  # legacy CT readings are instantaneous watts
            None,  # measurement_window_ms
            expected_interval_s,
            resolution_w,
            declared_by,
        ),
    )


def _backfill_endpoints(conn: sqlite3.Connection) -> None:
    """One endpoint per CT channel, baseline revision + one per factor interval."""
    if not _table_exists(conn, "ct_channels"):
        return
    channels = conn.execute(
        """SELECT ct_id, entity_id, name, ct_type, phase, polarity, enabled,
                  measurement_multiplier, resolution_w, expected_interval_s,
                  panel_id, metadata_json, updated_at
           FROM ct_channels ORDER BY ct_id"""
    ).fetchall()
    for row in channels:
        ct_id = _as_text(row[0]) or ""
        if not ct_id:
            continue
        created_at = _as_timestamp(row[12])
        ct_type = _as_text(row[3]) or "unknown"
        # A main CT measures a panel feed; everything else measures a circuit.
        # A circuit CT is never auto single-subject (DEC-05), so its status stays
        # CANDIDATE with no scope subject.
        scope = "panel_feed" if ct_type == "main" else "circuit"
        conn.execute(
            """INSERT OR IGNORE INTO measurement_endpoints(
                endpoint_id, kind, entity_id, energy_entity_id, display_name, scope,
                scope_status, scope_subject_id, panel_id, enabled, created_at, metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ct_id,
                "ct",
                _as_text(row[1]),
                None,
                _as_text(row[2]),
                scope,
                "candidate",
                None,
                _as_text(row[10]),
                int(bool(row[6])),
                created_at,
                _as_text(row[11]) or "{}",
            ),
        )

        factors = _ct_factors(conn, ct_id)
        # Revision 1 covers the open-ended period before the first factor
        # interval; if there are no factors it remains open-ended.  It starts at
        # a fixed early instant (not ``updated_at``, which is a topology-write
        # timestamp) so historical samples resolve to the legacy multiplier.
        baseline_until = factors[0]["valid_from"] if factors else None
        _insert_revision(
            conn,
            ct_id,
            1,
            valid_from=_BASELINE_START,
            valid_until=baseline_until,
            phase=_as_text(row[4]) or "unknown",
            polarity=_as_text(row[5]) or "normal",
            multiplier=float(row[7]) if row[7] is not None else 1.0,
            expected_interval_s=row[9],
            resolution_w=row[8],
            declared_by="migration",
        )
        for index, factor in enumerate(factors, start=2):
            _insert_revision(
                conn,
                ct_id,
                index,
                valid_from=factor["valid_from"],
                valid_until=factor["valid_until"],
                phase=_as_text(row[4]) or "unknown",
                polarity=_as_text(row[5]) or "normal",
                multiplier=float(factor["multiplier"]) if factor["multiplier"] is not None else 1.0,
                expected_interval_s=row[9],
                resolution_w=row[8],
                declared_by=factor["declared_by"],
            )


# --------------------------------------------------------------------------- #
# Physical device backfill
# --------------------------------------------------------------------------- #

#: Keys whose presence marks a discovery-generated ``dev_<ct>`` auto circuit.
#: Mirrors ``disaggregation/mapping.py::_is_ct_auto_circuit`` (lines 231-266).
_AUTO_CIRCUIT_MARKERS = ("shared_circuit", "ct_group", "pair_confidence")
_CONFIG_MARKERS = (
    "device_config",
    "materialised_from_config",
    "monitoring_level",
    "power_behavior",
    "connectivity",
)


def _is_legacy_ct_device(device_id: str, metadata: dict[str, Any], channel_ids: set[str]) -> bool:
    """Whether a legacy device is a discovery-generated CT circuit (D2 retires it)."""
    if any(key in metadata for key in _CONFIG_MARKERS):
        return False
    if any(key in metadata for key in _AUTO_CIRCUIT_MARKERS):
        return True
    return bool(device_id.startswith("dev_") and device_id[len("dev_") :] in channel_ids)


def _load_configs(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Tolerant ``{device_id: payload dict}`` from ``device_configurations``."""
    if not _table_exists(conn, "device_configurations"):
        return {}
    configs: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT device_id, payload_json FROM device_configurations").fetchall():
        device_id = _as_text(row[0])
        if device_id is None:
            continue
        payload = _json_object(row[1])
        configs[device_id] = payload
    return configs


def _contains_retired_role(config: dict[str, Any], metadata: dict[str, Any]) -> bool:
    """Whether a legacy payload carries a retired dedicated-CT/role vocabulary."""
    return (
        config.get("monitoring") == "dedicated"
        or metadata.get("monitoring_level") == "dedicated"
        or config.get("ct_type") == "dedicated_device"
    )


def _backfill_devices(conn: sqlite3.Connection) -> None:
    """Every legacy device id survives verbatim, flagged if it is a CT circuit."""
    if not _table_exists(conn, "devices"):
        return
    channel_ids = {
        _as_text(row[0]) or ""
        for row in conn.execute("SELECT ct_id FROM ct_channels").fetchall()
    } if _table_exists(conn, "ct_channels") else set()
    configs = _load_configs(conn)
    devices = conn.execute(
        """SELECT device_id, name, phase, parent_device_id, topology_status, enabled,
                  metadata_json, updated_at
           FROM devices ORDER BY device_id"""
    ).fetchall()
    for row in devices:
        device_id = _as_text(row[0]) or ""
        if not device_id:
            continue
        config = configs.get(device_id, {})
        metadata = _json_object(row[6])
        flags: dict[str, Any] = {}
        if _is_legacy_ct_device(device_id, metadata, channel_ids):
            flags["legacy_ct_device"] = 1
        if _contains_retired_role(config, metadata):
            flags["legacy_retired_role"] = 1
        merged_metadata = {**metadata, **flags}
        ha_device_id = _as_text(config.get("ha_device_id"))
        conn.execute(
            """INSERT OR IGNORE INTO physical_devices(
                device_id, name, device_type, load_shape, sign_class, ha_device_id, area_id,
                origin, created_at, retired_at, metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                device_id,
                _as_text(row[1]) or device_id,
                _as_text(config.get("device_type")),
                _as_text(config.get("load_shape")),
                "consumer",
                ha_device_id,
                _as_text(config.get("area_id")),
                "migration",
                _as_timestamp(row[7]),
                None,
                json.dumps(merged_metadata),
            ),
        )


# --------------------------------------------------------------------------- #
# HA entity + link backfill
# --------------------------------------------------------------------------- #

#: metadata / payload keys that name runtime state entities, mapped to a role.
_STATE_KEYS = ("state_entity_ids", "link_state_entity_ids", "config_load_entity_ids")
_MODE_KEYS = ("mode_entity_ids",)
_CONTROL_KEYS = ("controllable_entity_ids",)


def _backfill_ha_entities(conn: sqlite3.Connection) -> None:
    """Consolidate the four legacy HA-entity representations into two tables.

    ``ha_entity_bindings`` (role projection), ``DeviceTopology.metadata`` keys
    (``state_entity_ids``, ``link_state_entity_ids``, ``config_load_entity_ids``)
    and ``DeviceConfiguration`` fields (``state_entity_ids``, ``mode_entity_ids``,
    ``controllable_entity_ids``) all become ``(entity, device, role)`` links.  A
    link key is ``(entity_id, target_kind, target_id, valid_from)`` -- the table's
    primary key -- so the same entity declared through two stores collapses to
    one row.
    """
    if not _table_exists(conn, "devices"):
        return
    configs = _load_configs(conn)
    devices = conn.execute(
        "SELECT device_id, metadata_json, updated_at FROM devices ORDER BY device_id"
    ).fetchall()
    device_times = {_as_text(row[0]) or "": _as_timestamp(row[2]) for row in devices}

    # (entity_id, target_id, role, valid_from) from every store.
    candidates: list[tuple[str, str, str, str]] = []
    if _table_exists(conn, "ha_entity_bindings"):
        for row in conn.execute(
            "SELECT entity_id, device_id, role FROM ha_entity_bindings ORDER BY entity_id, device_id"
        ).fetchall():
            entity_id, device_id = _as_text(row[0]), _as_text(row[1])
            if entity_id and device_id:
                candidates.append(
                    (
                        entity_id,
                        device_id,
                        _valid_role(row[2]),
                        device_times.get(device_id, _FALLBACK_TIME),
                    )
                )

    for row in devices:
        device_id = _as_text(row[0])
        if device_id is None:
            continue
        valid_from = device_times.get(device_id, _FALLBACK_TIME)
        metadata = _json_object(row[1])
        config = configs.get(device_id, {})
        # DeviceTopology.metadata keys live in ``devices.metadata_json``;
        # DeviceConfiguration fields live in the config payload.  The config's
        # ``state_entity_ids`` carries the user's runtime declaration too.
        sources: list[tuple[Iterable[str], str]] = [
            (_string_list(metadata.get(key)), "load_state") for key in _STATE_KEYS
        ]
        sources.append((_string_list(config.get("state_entity_ids")), "load_state"))
        sources += [(_string_list(config.get(key)), "mode_only") for key in _MODE_KEYS]
        sources += [(_string_list(config.get(key)), "unknown") for key in _CONTROL_KEYS]
        for entities, role in sources:
            for raw in entities:
                if (entity_id := _as_text(raw)) is not None:
                    candidates.append((entity_id, device_id, role, valid_from))

    seen: set[tuple[str, str, str, str]] = set()
    links: list[dict[str, Any]] = []
    for entity_id, device_id, role, valid_from in candidates:
        key = (entity_id, "device", device_id, valid_from)
        if key in seen:
            continue
        seen.add(key)
        links.append(
            {
                "entity_id": entity_id,
                "target_kind": "device",
                "target_id": device_id,
                "role": role,
                "valid_from": valid_from,
            }
        )
    # An entity's HA device is the one declared on the engine device it is
    # linked to (when the config payload carries ``ha_device_id``).  This is what
    # lets ``subjects_for_ha_device`` resolve an HA device to its subjects.
    device_to_ha: dict[str, str] = {}
    for device_id, config in configs.items():
        if (ha_id := _as_text(config.get("ha_device_id"))) is not None:
            device_to_ha[device_id] = ha_id
    # HA entities have no first/last-seen timestamp in the legacy schema; use the
    # owning device's update time as a conservative evidence window.
    entity_times: dict[str, str] = {}
    entity_ha_device: dict[str, str | None] = {}
    for link in links:
        eid = link["entity_id"]
        stamp = link["valid_from"]
        if eid not in entity_times or stamp < entity_times[eid]:
            entity_times[eid] = stamp
        if entity_ha_device.get(eid) is None:
            entity_ha_device[eid] = device_to_ha.get(link["target_id"])
    for entity_id, stamp in sorted(entity_times.items()):
        conn.execute(
            """INSERT OR IGNORE INTO ha_entities(entity_id, domain, ha_device_id,
               first_seen, last_seen) VALUES(?,?,?,?,?)""",
            (entity_id, _domain(entity_id), entity_ha_device.get(entity_id), stamp, stamp),
        )
    for link in links:
        conn.execute(
            """INSERT OR IGNORE INTO ha_entity_links(
                entity_id, target_kind, target_id, role, valid_from, valid_until, declared_by)
            VALUES(?,?,?,?,?,?,?)""",
            (
                link["entity_id"],
                link["target_kind"],
                link["target_id"],
                link["role"],
                link["valid_from"],
                None,
                "migration",
            ),
        )


# --------------------------------------------------------------------------- #
# Migration object
# --------------------------------------------------------------------------- #


def _apply(conn: sqlite3.Connection) -> None:
    """Backfill inside the migration transaction, parent rows first."""
    _backfill_endpoints(conn)
    _backfill_devices(conn)
    _backfill_ha_entities(conn)


MIGRATION = Migration(
    version=13,
    name="entities",
    statements=_STATEMENTS,
    apply=_apply,
)

__all__ = ["MIGRATION", "TIERS"]
