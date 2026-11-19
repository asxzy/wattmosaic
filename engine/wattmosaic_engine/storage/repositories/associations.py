"""Association evidence: typed topology edges and per-CT measurement factors.

Both tables are *declarations with history*, which is why they live together:
``topology_relations`` is the typed edge set (``DEVICE_CT_ASSOCIATION``,
``ELECTRICAL_PARENT_CHILD``, ...) and ``ct_measurement_factors`` is the
time-scoped calibration history.  Neither is ever rewritten in place by a later
pass -- a factor row is a validity interval, an edge is upserted by id.

Migrations 14/15 (S14/S15) add the v3 edge and mapping-edge tables; these stay
readable until Z1 renames them.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from wattmosaic_engine.electrical._declared_shapes import RelationKindDecl

from ...contracts import MeasurementFactor, TopologyRelation, validate_factor_intervals
from ..repositories import Repository, enum_value, iso


class AssociationsRepository(Repository):
    # -- typed topology relations (contract 1.6.0) ------------------------- #
    def insert_topology_relation(self, relation: TopologyRelation) -> None:
        """Upsert one typed edge.  The relation id is the identity."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO topology_relations(relation_id, kind, subject_id, object_id,
                status, confidence, evidence_json, requires_confirmation, declared_by,
                algorithm_version, valid_from, valid_to, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(relation_id) DO UPDATE SET
                kind=excluded.kind, subject_id=excluded.subject_id,
                object_id=excluded.object_id, status=excluded.status,
                confidence=excluded.confidence, evidence_json=excluded.evidence_json,
                requires_confirmation=excluded.requires_confirmation,
                declared_by=excluded.declared_by, algorithm_version=excluded.algorithm_version,
                valid_from=excluded.valid_from, valid_to=excluded.valid_to""",
                (
                    relation.relation_id,
                    enum_value(relation.kind),
                    relation.subject_id,
                    relation.object_id,
                    enum_value(relation.status),
                    relation.confidence,
                    json.dumps(relation.evidence, default=str),
                    int(bool(relation.requires_confirmation)),
                    relation.declared_by,
                    relation.algorithm_version,
                    iso(relation.valid_from),
                    iso(relation.valid_to),
                    iso(relation.created_at),
                ),
            )

    def relations_of_kind(self, kind: RelationKindDecl | str) -> list[dict[str, Any]]:
        """Every stored edge of one kind, so kinds cannot be conflated by query."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM topology_relations WHERE kind=? ORDER BY created_at ASC",
                (enum_value(kind),),
            ).fetchall()
        out = [dict(r) for r in rows]
        for row in out:
            row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
            row["requires_confirmation"] = bool(row["requires_confirmation"])
        return out

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

    def factors_for_ct(self, ct_id: str) -> list[dict[str, Any]]:
        """Every factor interval for a CT, oldest ``effective_from`` first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ct_measurement_factors WHERE ct_id=? "
                "ORDER BY effective_from ASC, id ASC",
                (ct_id,),
            ).fetchall()
        return [dict(r) for r in rows]


__all__ = ["AssociationsRepository"]
