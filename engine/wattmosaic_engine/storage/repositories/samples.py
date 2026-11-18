"""Raw per-reader samples: the ``ct_samples`` table.

One ring of readings per measurement endpoint.  Until migration 16 this table
holds only the four columns a caller reads; S16 adds the rest of the HB §2.1
measurement as nullable columns and extends :meth:`SamplesRepository.insert_ct_samples`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ..repositories import SAMPLE_FETCH_CHUNK, Repository, as_utc, iso


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
