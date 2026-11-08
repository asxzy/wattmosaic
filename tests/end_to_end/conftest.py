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
def engine_db(tmp_db_path, sample_topology):
    """A real ``Database`` initialised with the audited topology."""
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    topology = sample_topology.to_contract_topology()
    db.save_topology(topology, note="e2e fixture")
    try:
        yield db, topology
    finally:
        db.close()


def measurement_rows(record: dict) -> list[dict]:
    """Contract-shaped sample rows for one simulator record."""
    rows = []
    for ct_id, m in record["measurements"].items():
        rows.append(
            {
                "ct_id": ct_id,
                "entity_id": m["entity_id"],
                "value_w": float(m["value_w"]),
                "raw_value": float(m["value_w"]),
                "unit": "W",
                "state_timestamp": dt.datetime.fromisoformat(m["state_timestamp"]),
                "received_at": dt.datetime.fromisoformat(m["received_at"]),
                "physical_timestamp": (
                    dt.datetime.fromisoformat(m["physical_timestamp"])
                    if m.get("physical_timestamp")
                    else None
                ),
                "quality": m["quality"],
                "repeated": m["repeated"],
                "sequence": m["sequence"],
            }
        )
    return rows
