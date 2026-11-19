"""End-to-end fixtures.

Kept separate from the repository-wide ``tests/conftest.py`` so the generic
fixtures stay free of engine-specific setup.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_ROOT = REPO_ROOT / "engine"
for path in (REPO_ROOT, ENGINE_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture()
def engine_db(tmp_db_path):
    """A real, freshly migrated ``Database``.

    The legacy contract topology (and ``save_topology``) was removed with the
    contract-3.0.0 cutover, so the fixture no longer seeds one; storage tests
    write v3 rows through ``db.repos``.
    """
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        yield db
    finally:
        db.close()


def measurement_readings(record: dict) -> list:
    """``ReaderMeasurement`` rows for one simulator record (one per reader)."""
    from wattmosaic_engine.contracts import (
        MeasurementBasis,
        MeasurementType,
        QualityFlag,
        ReaderMeasurement,
    )

    readings = []
    for ct_id, m in record["measurements"].items():
        readings.append(
            ReaderMeasurement(
                reader_id=ct_id,
                observed_at=dt.datetime.fromisoformat(m["state_timestamp"]),
                received_at=dt.datetime.fromisoformat(m["received_at"]),
                active_power_w=float(m["value_w"]),
                phase="unknown",
                measurement_type=MeasurementType.INSTANTANEOUS,
                calibration_version="sim-1",
                measurement_basis=MeasurementBasis.CT_MEASURED.value,
                quality_flags=[QualityFlag.REPEATED] if m["repeated"] else [],
                sequence=int(m["sequence"]),
            )
        )
    return readings
