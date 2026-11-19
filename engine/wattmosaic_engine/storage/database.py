"""SQLite persistence layer (WAL, versioned migrations, bounded retention).

**Shape after X05.**  This module keeps only what is genuinely global to the one
connection:

* the connection, its PRAGMAs and the ``RLock`` that guards it;
* the reader seam (:meth:`Database._reader`), which opens a short-lived
  ``query_only`` connection for bulk scans so they never block ``/health``;
* the migration + pre-migration backup lifecycle (:meth:`Database.__init__`,
  :meth:`Database.reset`);
* the latest-results cache generation counter, which every writer bumps;
* thin **delegating** methods with the old names, so no caller changes.

The SQL itself lives in ``storage/repositories/<aggregate>.py``, one module per
aggregate.  Later storage tasks (S13-S19) own one whole module each and add their
repository as ``db.repos.<name>`` without editing this file -- see
``storage/repositories/__init__.py``.

**Thread-safety.**  A single writer ``Database`` instance is created per process.
Its connection is guarded by ``threading.RLock``; every accessor takes the lock,
so concurrent use from a worker thread (``asyncio.to_thread``) cannot corrupt it.
Bulk reads that would otherwise hold that lock for hundreds of milliseconds open
a short-lived, ``query_only`` read connection instead -- WAL lets readers and the
writer proceed independently, so a long scan never blocks ``/health``.

**Pre-migration backup (DEC-02).**  Before applying any migration numbered 13 or
higher, the database file is checkpointed and copied to
``<db>.pre-v<NNN>.<UTC timestamp>.bak``.  The newest three backups are kept.
That is the whole rollback procedure for HB §16.8: there are no down-migrations,
so a rollback is "reinstall the previous version and restore the file".
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..contracts import (
    Circuit,
    DeviceConfiguration,
    DeviceConfigurationSet,
    EventIsolation,
    InferenceRecord,
    MeasurementFactor,
    ModelStatus,
    ModelType,
    ResidualPower,
    TopologyRelation,
    utcnow,
)

# ZI: the delegating methods below keep their legacy-compatible signatures for
# the shared callers owned by other areas (topology service, learning, tests).
# The shared legacy contract models are reached through the legacy module
# namespace so this area binds no legacy contract name; ZC removes them.
from ..contracts import legacy as _legacy
from .migrations import LATEST_VERSION, apply_migrations, current_version, pending_migrations
from .repositories import Repositories, backup_pattern, iso
from .repositories.topology import TopologyRepository

_LOGGER = logging.getLogger(__name__)

#: First schema version whose migration is destructive enough to require a
#: pre-migration backup (DEC-02).  Versions 1-11 shipped without one and their
#: upgrades are additive on every released shape; 13 is the first canonical-model
#: rebuild.
BACKUP_FROM_VERSION = 13

#: How many pre-migration backups to keep per database file.
BACKUP_KEEP = 3


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Record whether there was a real database to preserve *before* opening
        # the connection (which creates an empty file).  The backup decision must
        # not confuse "sqlite just made this file" with "the operator has data".
        # A zero-byte or brand-new file, or a file whose ``user_version`` is 0,
        # has nothing worth rolling back to.
        self._preexisting_file = self.path.exists() and self.path.stat().st_size > 0
        # ``check_same_thread=False`` lets a reader thread use this connection.
        # That is safe *only* because every accessor below executes inside
        # ``self._lock``: SQLite serialises a single connection internally,
        # but ``sqlite3`` itself refuses cross-thread use by default.  The lock
        # (an ``RLock``) is what actually provides the mutual exclusion, and it
        # is acquired on the loop thread and the reader thread alike.
        self._conn = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        # Latest-results cache (see ``latest_inference_results``).  ``None`` means
        # "no valid cached snapshot"; the generation counter lets a reader that
        # raced a write detect that its result is stale and avoid caching it.
        self._latest_results_cache: list[dict[str, Any]] | None = None
        self._cache_generation: int = -1  # never equal to a real generation
        self._write_generation: int = 0
        # DEC-02: back the file up before a migration that rebuilds tables.  The
        # check runs on the *pre-migration* version, so a database that is
        # already current is never backed up on every start.
        self.backup_path: Path | None = self._backup_before_migration()
        self.schema_version = apply_migrations(self._conn)
        self.repos = Repositories(self)

    def _configure(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA wal_autocheckpoint=1000")

    @contextlib.contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        """A short-lived read-only connection for bulk reads.

        The shared ``self._conn`` is guarded by ``self._lock``, so any query that
        runs on it blocks every other accessor for its whole duration -- including
        ``ping()`` on the ``/health`` path.  A 12–24 h ``ct_samples`` scan takes
        hundreds of milliseconds, which turned a 5 ms health probe into a
        multi-second one.

        The database is in WAL mode, where readers and the single writer do not
        block each other, so a second connection can read while the writer
        connection keeps persisting.  ``PRAGMA query_only=ON`` makes "this
        connection never writes" enforced rather than merely intended, and the
        connection is closed on exit so no handle leaks across requests.

        Falls back to the shared connection when there is no file to open
        (e.g. a ``:memory:`` database), where a second connection would not see
        the same data.
        """
        if not self.path.exists():
            with self._lock:
                yield self._conn
            return
        conn = sqlite3.connect(
            str(self.path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
        finally:
            conn.close()

    # -- lifecycle -------------------------------------------------------- #
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> None:
        """Cheap liveness probe for health endpoints.

        Deliberately does **not** run ``PRAGMA integrity_check``.  That pragma
        reads the entire database file — seconds on a few hundred MB and far
        longer on slow storage — so it must never sit on a probe path that runs
        every few seconds.  It raises if the connection is unusable.
        """
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    def integrity_check(self) -> dict[str, Any]:
        """Full database verification. Expensive: call at startup/diagnostics only.

        ``PRAGMA integrity_check`` already performs every check ``quick_check``
        does, so this is a whole-file scan.  Never call it from a health probe.
        """
        with self._lock:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
            quick = self._conn.execute("PRAGMA quick_check").fetchone()
        return {
            "integrity": row[0] if row else "unknown",
            "quick_check": quick[0] if quick else "unknown",
            "schema_version": self.schema_version,
            "latest_schema_version": LATEST_VERSION,
            "path": str(self.path),
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def checkpoint(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # -- pre-migration backup (DEC-02) ------------------------------------ #
    def _is_memory(self) -> bool:
        return str(self.path) == ":memory:"

    def _backup_before_migration(self) -> Path | None:
        """Copy the file aside when a migration numbered 13+ is pending.

        Returns the backup path, or ``None`` when nothing was copied (a fresh,
        empty or unversioned database, an in-memory database, or no pending
        migration at or above :data:`BACKUP_FROM_VERSION`).

        The steps mirror :meth:`reset` steps 1-2, for the same reason: a WAL-mode
        file is only self-contained after a checkpoint, and the copy has to exist
        *before* the first destructive statement runs.  If the copy fails the
        exception propagates -- starting up against an unbacked-up destructive
        migration is worse than not starting up.
        """
        if self._is_memory():
            return None
        # A backup is only a rollback point for data that already existed.  A
        # fresh install (no file before this constructor), a zero-byte file, or
        # a database still at ``user_version`` 0 has nothing to roll back to.
        # Skip in all three cases instead of copying an empty/unversioned file.
        if not self._preexisting_file:
            return None
        if current_version(self._conn) < 1:
            return None
        pending = [m for m in pending_migrations(self._conn) if m.version >= BACKUP_FROM_VERSION]
        if not pending:
            return None
        target = max(m.version for m in pending)
        self.checkpoint()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = Path(f"{self.path}.pre-v{target:03d}.{stamp}.bak")
        suffix = 0
        while backup.exists():
            suffix += 1
            backup = Path(f"{self.path}.pre-v{target:03d}.{stamp}.{suffix}.bak")
        shutil.copy2(self.path, backup)
        # Carry over any remaining sidecars so the backup is self-contained even
        # if a writer re-created them between the checkpoint and the copy.
        for sidecar in ("-wal", "-shm"):
            side = Path(f"{self.path}{sidecar}")
            if side.exists():
                shutil.copy2(side, Path(f"{backup}{sidecar}"))
        _LOGGER.info(
            "pre-migration backup of %s (schema %s -> %s) written to %s",
            self.path,
            current_version(self._conn),
            target,
            backup,
        )
        self._rotate_backups()
        return backup

    def _rotate_backups(self) -> list[Path]:
        """Keep the newest :data:`BACKUP_KEEP` pre-migration backups, delete the rest.

        Ordered by modification time and then name: two backups taken in the same
        second are told apart by their ``.N`` disambiguating suffix, which does
        *not* sort in creation order (``…Z.1.bak`` sorts before ``…Z.bak``).
        """
        backups = sorted(
            self.path.parent.glob(backup_pattern(self.path)),
            key=lambda p: (p.stat().st_mtime_ns if p.exists() else 0, p.name),
        )
        removed: list[Path] = []
        for old in backups[:-BACKUP_KEEP] if len(backups) > BACKUP_KEEP else []:
            for candidate in (old, Path(f"{old}-wal"), Path(f"{old}-shm")):
                candidate.unlink(missing_ok=True)
            removed.append(old)
        return removed

    def reset(self) -> dict[str, Any]:
        """Wipe **all** app data and re-initialise a brand-new migrated schema.

        This is the backend half of the destructive "Reset DB" feature.  It is
        deliberately a *file-level* reset rather than a sequence of
        ``DELETE FROM`` statements: dropping/emptying tables one by one can miss
        WAL sidecars, ``sqlite_sequence`` counters and FTS/auxiliary artifacts,
        leaving a half-wiped database.  Replacing the file guarantees the result
        is byte-for-byte a fresh install.

        Safety order matters:

        1. :meth:`checkpoint` folds the WAL into the main file, so the backup is
           a single self-contained copy even in WAL mode.
        2. A timestamped ``<db>.reset-backup.<UTC>`` copy (including any WAL/SHM
           sidecars that remain) is created **before** anything is removed.  The
           reset is a no-op if this fails.
        3. Under the lock, the connection is closed, every DB file/sidecar is
           removed, a new connection is opened **in place on this object** and
           all migrations are re-applied.  Rebinding ``self._conn`` (rather than
           constructing a new ``Database``) is essential: every engine component
           holds a reference to this *same* object, so they all transparently
           see the fresh database afterwards.

        The method is idempotent-safe: resetting an already-fresh database just
        makes another backup and re-migrates it again.  An in-memory database
        (``:memory:``) has no file to back up, so it is rejected explicitly
        rather than silently doing something unsafe.

        Returns a confirmation dict containing the backup path, the new schema
        version and the latest schema version.  Raises ``RuntimeError`` for an
        unsupported (in-memory) database.
        """
        if self._is_memory():
            raise RuntimeError("reset is not supported for an in-memory database")

        # 1. Checkpoint so the main file is complete and the sidecars can be
        #    ignored/removed safely after the backup.
        self.checkpoint()

        # 2. Timestamped safety backup, created before any destructive step.
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = Path(f"{self.path}.reset-backup.{stamp}")
        # Avoid a (very unlikely) same-microsecond collision.
        suffix = 0
        while backup.exists():
            suffix += 1
            backup = Path(f"{self.path}.reset-backup.{stamp}.{suffix}")
        shutil.copy2(self.path, backup)
        # Carry over any remaining sidecars so the backup is self-contained even
        # if a writer re-created them between the checkpoint and the copy.
        for sidecar in ("-wal", "-shm"):
            side = Path(f"{self.path}{sidecar}")
            if side.exists():
                shutil.copy2(side, Path(f"{backup}{sidecar}"))

        # 3. Close, remove every DB file, reopen in place, re-migrate.  All under
        #    the lock so no other accessor can observe a half-reset connection.
        with self._lock:
            self._conn.close()
            for target in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                target.unlink(missing_ok=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.path), timeout=30.0, isolation_level=None, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._configure()
            self.schema_version = apply_migrations(self._conn)
            # A reset replaces the whole database under this object: a cache
            # built from the old file would serve rows that no longer exist.
            self.invalidate_latest_results()

        return {
            "reset": True,
            "backup_path": str(backup),
            "schema_version": self.schema_version,
            "latest_schema_version": LATEST_VERSION,
        }

    # -- latest-results cache invalidation -------------------------------- #
    def invalidate_latest_results(self) -> None:
        """Drop the cached newest-per-device map and bump the write generation.

        Called by every writer that can change what ``latest_inference_results``
        returns (an inference insert, a prune, a reset).  Kept on ``Database``
        because the cache itself lives here: the read in
        ``repositories/inference.py`` consults these fields directly, and a
        repository created after a reset must see the same object.
        """
        self._write_generation += 1
        self._latest_results_cache = None

    # -- engine state ----------------------------------------------------- #
    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO engine_state(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, default=str), iso(utcnow())),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM engine_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # -- topology --------------------------------------------------------- #
    @staticmethod
    def _insertion_order(channels: Iterable[_legacy.CTChannel]) -> list[_legacy.CTChannel]:
        """Order channels so a parent is always written before its children.

        The rule lives on :class:`TopologyRepository` -- the SQL that needs it is
        there -- and is reached through the class rather than an instance because
        ``save_topology`` is a write and this ordering is pure.  Kept importable
        from ``Database`` because the migration-era code and tests name it here.
        """
        return TopologyRepository._insertion_order(channels)

    def save_topology(self, topology: _legacy.Topology, note: str = "") -> int:
        return self.repos.topology.save_topology(topology, note=note)

    def load_topology(self) -> _legacy.Topology | None:
        return self.repos.topology.load_topology()

    def upsert_circuit(self, circuit: Circuit) -> None:
        self.repos.topology.upsert_circuit(circuit)

    def circuits_all(self) -> list[dict[str, Any]]:
        return self.repos.topology.circuits_all()

    # -- device configuration --------------------------------------------- #
    def save_device_configurations(self, configurations: DeviceConfigurationSet) -> None:
        """Replace the stored per-device configuration set atomically."""
        self.repos.devices.save_device_configurations(configurations)

    def load_device_configurations(self) -> DeviceConfigurationSet:
        return self.repos.devices.load_device_configurations()

    def get_device_configuration(self, device_id: str) -> DeviceConfiguration | None:
        return self.repos.devices.get_device_configuration(device_id)

    # -- samples ---------------------------------------------------------- #
    def insert_ct_samples(self, rows: Iterable[dict[str, Any]]) -> int:
        return self.repos.samples.insert_ct_samples(rows)

    def ct_samples_between(
        self, start: datetime, end: datetime, ct_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return self.repos.samples.ct_samples_between(start, end, ct_ids)

    def latest_ct_sample(self, ct_id: str) -> dict[str, Any] | None:
        return self.repos.samples.latest_ct_sample(ct_id)

    def latest_ct_samples(self) -> dict[str, dict[str, Any]]:
        return self.repos.samples.latest_ct_samples()

    # -- events ----------------------------------------------------------- #
    def insert_device_event(self, event: dict[str, Any]) -> None:
        self.repos.events.insert_device_event(event)

    def recent_device_events(self, since: datetime, limit: int = 1000) -> list[dict[str, Any]]:
        return self.repos.events.recent_device_events(since, limit)

    def log_quality_event(
        self, code: str, message: str, severity: str = "warning", channel_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.repos.events.log_quality_event(code, message, severity, channel_id, details)

    def log_system_event(
        self, level: str, category: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        self.repos.events.log_system_event(level, category, message, details)

    # -- inference -------------------------------------------------------- #
    def insert_inference_results(self, results: Iterable[_legacy.InferenceResult]) -> int:
        return self.repos.inference.insert_inference_results(results)

    def latest_inference_results(self) -> list[dict[str, Any]]:
        """The newest result per device, cached across reads until the next write."""
        return self.repos.inference.latest_inference_results()

    def inference_history(
        self, device_id: str, since: datetime, limit: int = 5000
    ) -> list[dict[str, Any]]:
        return self.repos.inference.inference_history(device_id, since, limit)

    # -- versioned inference records (contract 1.6.0, migration 9) --------- #
    def insert_inference_record(self, record: InferenceRecord) -> None:
        """Append one revision.  A revision never overwrites its predecessor."""
        self.repos.records.insert_inference_record(record)

    def inference_records_for_device(
        self, device_id: str, since: datetime | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        return self.repos.records.inference_records_for_device(device_id, since, limit)

    def latest_inference_record(self, device_id: str) -> dict[str, Any] | None:
        return self.repos.records.latest_inference_record(device_id)

    def inference_records_for_topology_version(
        self, topology_version: int | str, limit: int = 5000
    ) -> list[dict[str, Any]]:
        return self.repos.records.inference_records_for_topology_version(topology_version, limit)

    # -- measurement factors (contract 1.6.0) ----------------------------- #
    def insert_measurement_factor(self, factor: MeasurementFactor) -> None:
        self.repos.associations.insert_measurement_factor(factor)

    def factors_for_ct(self, ct_id: str) -> list[dict[str, Any]]:
        return self.repos.associations.factors_for_ct(ct_id)

    # -- typed topology relations (contract 1.6.0) ------------------------ #
    def insert_topology_relation(self, relation: TopologyRelation) -> None:
        self.repos.associations.insert_topology_relation(relation)

    def relations_of_kind(self, kind: _legacy.RelationKind | str) -> list[dict[str, Any]]:
        return self.repos.associations.relations_of_kind(kind)

    # -- isolation verdicts (migration 10) --------------------------------- #
    def insert_isolation_verdict(
        self, verdict: EventIsolation, *, algorithm_version: str = ""
    ) -> None:
        self.repos.records.insert_isolation_verdict(verdict, algorithm_version=algorithm_version)

    def isolation_verdict(self, event_id: str) -> dict[str, Any] | None:
        return self.repos.records.isolation_verdict(event_id)

    def isolation_verdicts(
        self, since: datetime | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return self.repos.records.isolation_verdicts(since, limit)

    def resolve_evidence_ref(self, ref: str) -> bool:
        """Whether one ``evidence_refs`` entry names a row that still exists."""
        return self.repos.records.resolve_evidence_ref(ref)

    def _sample_exists(self, key: str) -> bool:
        return self.repos.samples.sample_exists(key)

    def _circuit_exists(self, circuit_id: str) -> bool:
        return self.repos.topology.circuit_exists(circuit_id)

    def _record_exists(self, record_id: str) -> bool:
        return self.repos.records.record_exists(record_id)

    def _relation_exists(self, relation_id: str) -> bool:
        return self.repos.records.relation_exists(relation_id)

    # -- residual power (contract 1.6.0) ---------------------------------- #
    def insert_residual_power(
        self,
        residual: ResidualPower,
        timestamp: datetime | None = None,
        reconciliation_id: int | None = None,
    ) -> None:
        self.repos.residual.insert_residual_power(residual, timestamp, reconciliation_id)

    def residual_history(
        self, since: datetime | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        return self.repos.residual.residual_history(since, limit)

    # -- reconciliation --------------------------------------------------- #
    def insert_reconciliation(self, rec: _legacy.Reconciliation) -> None:
        self.repos.residual.insert_reconciliation(rec)

    def latest_reconciliation(self) -> dict[str, Any] | None:
        return self.repos.residual.latest_reconciliation()

    # -- learning --------------------------------------------------------- #
    def insert_learning_sample(self, sample: _legacy.LearningSample) -> None:
        self.repos.learning.insert_learning_sample(sample)

    def learning_samples(
        self, device_id: str | None = None, accepted_only: bool = True
    ) -> list[dict[str, Any]]:
        return self.repos.learning.learning_samples(device_id, accepted_only)

    def count_learning_samples(self, device_id: str, accepted_only: bool = True) -> int:
        return self.repos.learning.count_learning_samples(device_id, accepted_only)

    # -- models ----------------------------------------------------------- #
    def save_model_version(self, model: _legacy.DeviceModelVersion, activate: bool = False) -> None:
        self.repos.models.save_model_version(model, activate=activate)

    def active_model(self, device_id: str, model_type: str | None = None) -> dict[str, Any] | None:
        return self.repos.models.active_model(device_id, model_type)

    def model_versions(self, device_id: str) -> list[dict[str, Any]]:
        return self.repos.models.model_versions(device_id)

    def set_model_status(
        self, device_id: str, model_type: str, version: int, status: ModelStatus
    ) -> None:
        self.repos.models.set_model_status(device_id, model_type, version, status)

    def set_model_frozen(self, device_id: str, frozen: bool) -> None:
        self.repos.models.set_model_frozen(device_id, frozen)

    def is_model_frozen(self, device_id: str) -> bool:
        return self.repos.models.is_model_frozen(device_id)

    def rollback_model(self, device_id: str, model_type: str) -> int | None:
        return self.repos.models.rollback_model(device_id, model_type)

    # -- aggregation / retention ----------------------------------------- #
    def upsert_power_aggregate(
        self, device_id: str, bucket_start: datetime, bucket_s: int, mean_w: float, min_w: float,
        max_w: float, count: int,
    ) -> None:
        self.repos.aggregates.upsert_power_aggregate(
            device_id, bucket_start, bucket_s, mean_w, min_w, max_w, count
        )

    def upsert_energy_aggregate(
        self, device_id: str, bucket_start: datetime, bucket_s: int, energy_kwh: float
    ) -> None:
        self.repos.aggregates.upsert_energy_aggregate(device_id, bucket_start, bucket_s, energy_kwh)

    def energy_total(self, device_id: str, bucket_s: int | None = None) -> float:
        """Sum persisted energy for exactly one bucket size.

        ``energy_aggregates`` stores the *same* physical energy once per
        resolution (default 300 s and 3600 s), so summing every row would count
        the same kWh once per bucket size.  ``bucket_s=None`` therefore picks
        the smallest persisted size (the most detailed series) and sums only
        that one.
        """
        return self.repos.aggregates.energy_total(device_id, bucket_s)

    def prune(self, raw_days: int = 30, aggregate_days: int = 730) -> dict[str, int]:
        """Delete rows past their horizon; never delete long-term state.

        Which tables are pruned, by which timestamp column and on which horizon,
        is read from :func:`wattmosaic_engine.storage.tiers.prunable_tables`
        rather than restated here, so the registry is the one declaration the
        retention policy reads (`REQ-STORAGE-001`).  The registry's horizon is
        authoritative for a table: a table classified in one tier but shipped on
        another keeps the shipped horizon (``device_events``, see the registry).

        Every table in the registry that the pass does not delete is **reported
        with a count of ``0`` and left untouched**, so "deleted nothing" is
        visible rather than merely absent.

        The signature and its defaults are unchanged: ``main.py``'s maintenance
        loop still calls ``prune(retention_days_raw, retention_days_aggregates)``
        and gets exactly the behaviour it had before.
        """
        return self.repos.aggregates.prune(raw_days, aggregate_days)

    def export_device_data(self, device_id: str) -> dict[str, Any]:
        return self.repos.aggregates.export_device_data(device_id)

    def stats(self) -> dict[str, Any]:
        return self.repos.aggregates.stats()


def default_parameters() -> _legacy.DeviceModelParameters:
    return _legacy.DeviceModelParameters()


__all__ = [
    "BACKUP_FROM_VERSION",
    "BACKUP_KEEP",
    "Database",
    "LATEST_VERSION",
    "ModelType",
    "apply_migrations",
    "current_version",
    "default_parameters",
    "iso",
]
