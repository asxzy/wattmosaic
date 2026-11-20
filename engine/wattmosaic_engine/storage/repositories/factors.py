"""Per-CT measurement factors: the ``ct_measurement_factors`` validity intervals.

A factor row is a time-scoped calibration declaration; it is never rewritten in
place by a later pass.  :class:`~wattmosaic_engine.electrical.factors.FactorRegistry`
reads it to scale a reading by the factor in force at the reading's time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...contracts import MeasurementFactor, validate_factor_intervals
from ..repositories import Repository, iso


class FactorsRepository(Repository):
    # -- measurement factors (contract 1.6.0) ------------------------------ #
    def insert_measurement_factor(self, factor: MeasurementFactor) -> None:
        """Append a validity interval, refusing an overlap for the same CT.

        The interval invariant is re-checked here against the stored history, not
        just within one caller-supplied list: storage is the last line of
        defence, and a historical reconstruction that resolves two overlapping
        factors would pick one arbitrarily.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing_rows = self._conn.execute(
                    "SELECT * FROM ct_measurement_factors WHERE ct_id=?",
                    (factor.ct_id,),
                ).fetchall()
                existing = [
                    MeasurementFactor(
                        ct_id=row["ct_id"],
                        multiplier=float(row["multiplier"]),
                        effective_from=datetime.fromisoformat(str(row["effective_from"])),
                        effective_to=None
                        if row["effective_to"] is None
                        else datetime.fromisoformat(str(row["effective_to"])),
                        applied_by=row["applied_by"],
                        declared_by=row["declared_by"],
                        note=row["note"],
                    )
                    for row in existing_rows
                ]
                validate_factor_intervals([*existing, factor])
                self._conn.execute(
                    """INSERT INTO ct_measurement_factors(ct_id, multiplier, effective_from,
                    effective_to, applied_by, declared_by, note) VALUES(?,?,?,?,?,?,?)""",
                    (
                        factor.ct_id,
                        float(factor.multiplier),
                        iso(factor.effective_from),
                        iso(factor.effective_to),
                        factor.applied_by,
                        factor.declared_by,
                        factor.note,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def count(self) -> int:
        """Number of factor rows (cheap cache-invalidation signal for the fold)."""
        with self._reader() as conn:
            row = conn.execute("SELECT COUNT(*) FROM ct_measurement_factors").fetchone()
        return int(row[0]) if row is not None else 0

    def factors_for_ct(self, ct_id: str) -> list[dict[str, Any]]:
        """Every factor interval for a CT, oldest ``effective_from`` first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ct_measurement_factors WHERE ct_id=? "
                "ORDER BY effective_from ASC, id ASC",
                (ct_id,),
            ).fetchall()
        return [dict(r) for r in rows]


__all__ = ["FactorsRepository"]
