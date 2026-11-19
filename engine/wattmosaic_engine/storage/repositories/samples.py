"""Raw per-reader samples: the ``ct_samples`` table.

One ring of readings per measurement endpoint.  Migration 16 adds the rest of
the HB §2.1 measurement as nullable columns; :meth:`SamplesRepository.insert_readings`
persists every field of a :class:`~wattmosaic_engine.contracts.ReaderMeasurement`.
The legacy :meth:`insert_ct_samples` stays for the pre-v3 ingest path.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ...contracts import (
    MeasurementBasis,
    MeasurementType,
    QualityFlag,
    ReaderMeasurement,
)
from ...contracts import utcnow as _utcnow
from ..repositories import SAMPLE_FETCH_CHUNK, Repository, as_utc, enum_value, iso


class SamplesRepository(Repository):
    def insert_ct_samples(self, rows: Iterable[dict[str, Any]]) -> int:
        payload = [
            (
                r["ct_id"], r["entity_id"], float(r["value_w"]), float(r["raw_value"]), r.get("unit", "W"),
                iso(r["state_timestamp"]), iso(r["received_at"]), iso(r.get("physical_timestamp")),
                r.get("quality", "good"), int(bool(r.get("repeated"))), int(r.get("sequence", 0)),
            )
            for r in rows
        ]
        if not payload:
            return 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    """INSERT INTO ct_samples(ct_id, entity_id, value_w, raw_value, unit, state_timestamp,
                    received_at, physical_timestamp, quality, repeated, sequence)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    payload,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(payload)

    # -- reader samples v2 (migration 16) ---------------------------------- #
    def insert_readings(self, readings: Iterable[ReaderMeasurement]) -> int:
        """Persist every HB §2.1 field of a batch of :class:`ReaderMeasurement`.

        ``ct_samples.value_w`` and ``raw_value`` are still ``NOT NULL`` (SQLite
        cannot relax that with ``ALTER TABLE``; Z1 may rebuild the table), so
        they keep a zero sentinel for the legacy reader.  The authoritative v2
        column ``active_power_v2_w`` is nullable: an energy-only reading keeps
        ``None`` there, and :meth:`reading_from_row` reads it in preference to
        the sentinel.  Absence is therefore preserved as ``None``, never turned
        into ``0`` (HB §18).  Every other HB §2.1 field is retained verbatim.
        """
        payload = [self._reading_row(r) for r in readings]
        if not payload:
            return 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    """INSERT INTO ct_samples(
                        ct_id, entity_id, value_w, raw_value, unit, state_timestamp, received_at,
                        physical_timestamp, quality, repeated, sequence,
                        active_power_v2_w, current_rms_a, voltage_rms_v, reactive_power_var,
                        power_factor, energy_wh, phase, leg, measurement_type,
                        measurement_window_ms, calibration_version, quality_flags_json,
                        measurement_basis, observed_at, voltage_reference_v)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    payload,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(payload)

    @staticmethod
    def _reading_row(reading: ReaderMeasurement) -> tuple[Any, ...]:
        """One ``ct_samples`` row from a :class:`ReaderMeasurement`."""
        power = reading.active_power_w
        flags = [enum_value(flag) for flag in reading.quality_flags]
        return (
            reading.reader_id,                       # ct_id
            reading.reader_id,                       # entity_id (no separate field on the contract)
            0.0 if power is None else float(power),  # value_w (legacy NOT NULL)
            0.0 if power is None else float(power),  # raw_value (legacy NOT NULL)
            "W",                                     # unit
            iso(reading.observed_at),                # state_timestamp
            iso(reading.received_at),
            None,                                    # physical_timestamp
            "good",                                  # quality (legacy; flags are authoritative)
            int(any(f == "repeated" for f in flags)),
            int(reading.sequence),
            power,                                   # active_power_v2_w (nullable, authoritative)
            reading.current_rms_a,
            reading.voltage_rms_v,
            reading.reactive_power_var,
            reading.power_factor,
            reading.energy_wh,
            reading.phase,
            reading.leg,
            enum_value(reading.measurement_type),
            reading.measurement_window_ms,
            reading.calibration_version,
            json.dumps(flags),
            enum_value(reading.measurement_basis),
            iso(reading.observed_at),
            reading.voltage_reference_v,
        )

    @staticmethod
    def reading_from_row(row: sqlite3.Row | dict[str, Any]) -> ReaderMeasurement:
        """Rebuild a :class:`ReaderMeasurement` from a v2 ``ct_samples`` row."""
        data = dict(row)
        flags = json.loads(data.get("quality_flags_json") or "[]")
        phase = data.get("phase") or "unknown"
        return ReaderMeasurement(
            reader_id=data["ct_id"],
            observed_at=as_utc(data.get("observed_at") or data["state_timestamp"]),
            received_at=as_utc(data["received_at"]),
            active_power_w=(
                data["active_power_v2_w"]
                if "active_power_v2_w" in data
                else data["value_w"]
            ),
            current_rms_a=data.get("current_rms_a"),
            voltage_rms_v=data.get("voltage_rms_v"),
            reactive_power_var=data.get("reactive_power_var"),
            power_factor=data.get("power_factor"),
            energy_wh=data.get("energy_wh"),
            phase=phase,
            leg=data.get("leg"),
            voltage_reference_v=data.get("voltage_reference_v"),
            measurement_type=MeasurementType(data.get("measurement_type") or "instantaneous"),
            measurement_window_ms=data.get("measurement_window_ms"),
            calibration_version=data.get("calibration_version") or "unknown",
            measurement_basis=MeasurementBasis(data.get("measurement_basis") or "unknown").value,
            quality_flags=[QualityFlag(f) for f in flags],
            sequence=int(data.get("sequence") or 0),
        )

    def readings_between(
        self, reader_id: str, start: datetime, end: datetime
    ) -> list[ReaderMeasurement]:
        """Reader measurements for one endpoint in a closed ``observed_at`` window."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ct_samples WHERE ct_id=? AND observed_at >= ? AND observed_at <= ? "
                "ORDER BY observed_at, sequence",
                (reader_id, iso(start), iso(end)),
            ).fetchall()
        return [self.reading_from_row(row) for row in rows]

    def latest_reading(self, reader_id: str) -> ReaderMeasurement | None:
        """The newest persisted reader measurement for one endpoint, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ct_samples WHERE ct_id=? ORDER BY id DESC LIMIT 1", (reader_id,)
            ).fetchone()
        return None if row is None else self.reading_from_row(row)

    def ct_samples_between(
        self, start: datetime, end: datetime, ct_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Raw samples in a closed window, oldest windows first is *not* promised.

        Only the four columns a caller actually reads are selected: ``SELECT *``
        materialises ``id``, ``entity_id``, ``raw_value``, ``unit``,
        ``physical_timestamp``, ``quality``, ``repeated`` and ``sequence`` for
        every row and then throws them away.  At 77 000 rows for a 12 h window
        that was ~40 % of the query's cost.

        ``ORDER BY received_at`` is gone too.  It sorted 77 000 rows the caller
        immediately re-sorted per channel, and no caller depends on the order:
        ``EngineCore.nesting_candidates`` sorts each channel series by
        timestamp itself, and the storage tests only assert membership and that
        every returned row is inside the window.  **If you add a caller that
        needs ordering, sort in that caller** rather than paying for it here.
        """
        query = (
            "SELECT ct_id, value_w, state_timestamp, received_at "
            "FROM ct_samples WHERE received_at >= ? AND received_at <= ?"
        )
        params: list[Any] = [iso(start), iso(end)]
        if ct_ids:
            query += f" AND ct_id IN ({','.join('?' * len(ct_ids))})"
            params.extend(ct_ids)
        # A dedicated read connection, not ``self._conn``: see ``Database._reader``.
        # The lock-guarded shared connection would make this multi-hundred-
        # millisecond scan block ``ping()`` (and therefore ``/health``) for its
        # whole duration.
        #
        # ``fetchmany`` in chunks, not ``fetchall``: SQLite's ``fetchall`` is one
        # long C call that holds the GIL for the entire 77 000-row fetch, which
        # showed up as a ~250 ms stall on a concurrent ``/health`` probe even
        # though the query itself ran in a worker thread.  Chunking lets the loop
        # run between chunks; measured worst-case probe latency drops to ~2 ms.
        out: list[dict[str, Any]] = []
        with self._reader() as conn:
            cursor = conn.execute(query, params)
            while True:
                chunk = cursor.fetchmany(SAMPLE_FETCH_CHUNK)
                if not chunk:
                    break
                out.extend(dict(row) for row in chunk)
        return out

    def latest_ct_sample(self, ct_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ct_samples WHERE ct_id=? ORDER BY id DESC LIMIT 1", (ct_id,)
            ).fetchone()
        return dict(row) if row else None

    def latest_ct_samples(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT s.* FROM ct_samples s JOIN (SELECT ct_id, MAX(id) AS mid FROM ct_samples GROUP BY ct_id) m
                ON s.ct_id = m.ct_id AND s.id = m.mid"""
            ).fetchall()
        return {r["ct_id"]: dict(r) for r in rows}

    # -- reader state snapshot (P2) --------------------------------------- #
    #: Key in the ``engine_state`` table for the serialized ReaderStateStore.
    READER_STATE_KEY = "reader_state_v1"

    def put_reader_state(self, blob: bytes) -> None:
        """Persist the :mod:`~wattmosaic_engine.realtime.reader_state` blob.

        The blob is an opaque, checksummed byte string produced by
        :meth:`ReaderStateStore.snapshot_state`; it is stored verbatim as a
        base64 string in the existing ``engine_state`` table (no migration
        needed, since P2 owns no table).  Passing ``None`` removes the key, so
        a store that has never held data never restores a stale snapshot.
        """
        import base64

        if blob is None:
            with self._lock:
                self._conn.execute(
                    "DELETE FROM engine_state WHERE key=?", (self.READER_STATE_KEY,)
                )
            return
        value = base64.b64encode(bytes(blob)).decode("ascii")
        with self._lock:
            self._conn.execute(
                "INSERT INTO engine_state(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (self.READER_STATE_KEY, json.dumps(value), iso(_utcnow())),
            )

    def get_reader_state(self) -> bytes | None:
        """Return the stored reader-state blob, or ``None`` when there is none."""
        import base64

        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM engine_state WHERE key=?", (self.READER_STATE_KEY,)
            ).fetchone()
        if row is None:
            return None
        return base64.b64decode(json.loads(row["value"]).encode("ascii"))

    def sample_exists(self, key: str) -> bool:
        """``sample:<ct_id>@<iso timestamp>`` -> a stored raw sample.

        The pair is what identifies a sample to a reader; the row id is an
        implementation detail that changes between databases, so it must not be
        the reference.
        """
        if "@" not in key:
            return False
        ct_id, stamp = key.split("@", 1)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM ct_samples WHERE ct_id=? AND state_timestamp=? LIMIT 1",
                (ct_id, iso(as_utc(stamp))),
            ).fetchone()
        return row is not None


__all__ = ["SamplesRepository"]
