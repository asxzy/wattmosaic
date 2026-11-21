"""E5 §2: companion V/I/PF ingestion is routed, bounded and never a reading.

Companion entities update a per-reader record; they never become readers and
never emit a power reading.  A power sample gets companion fields only from a
value no more than 30 s older.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.ingestion.companions import (
    COMPANION_MAX_AGE_S,
    CompanionStore,
    entity_to_reader,
)

T0 = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)


def test_declaration_routing_inverts_entity_ids() -> None:
    decl = {
        "r1": {
            "voltage_entity_id": "sensor.r1_v",
            "current_entity_id": "sensor.r1_i",
            "power_factor_entity_id": "sensor.r1_pf",
        }
    }
    assert entity_to_reader(decl) == {
        "sensor.r1_v": "r1",
        "sensor.r1_i": "r1",
        "sensor.r1_pf": "r1",
    }


def test_latest_values_fill_only_within_30s() -> None:
    store = CompanionStore()
    store.observe(
        "r1", voltage_v=120.0, current_a=5.0, power_factor=0.9, observed_at=T0
    )
    # Just inside the window: all fields populated.
    assert store.values_at("r1", T0 + timedelta(seconds=20)) == (120.0, 5.0, 0.9)
    # Just outside: absent (None), never a stale 0.
    assert store.values_at("r1", T0 + timedelta(seconds=COMPANION_MAX_AGE_S + 1)) == (
        None,
        None,
        None,
    )
    # A future value (skewed clock) is also absent.
    assert store.values_at("r1", T0 - timedelta(seconds=1)) == (None, None, None)


def test_missing_reader_is_absent_not_zero() -> None:
    store = CompanionStore()
    assert store.values_at("unknown", T0) == (None, None, None)
    assert store.latest("unknown") is None


def test_observe_is_per_reader_and_merges_changed_quantity() -> None:
    store = CompanionStore()
    store.observe("r1", voltage_v=120.0, observed_at=T0)
    store.observe("r1", current_a=5.0, observed_at=T0 + timedelta(seconds=5))
    record = store.latest("r1")
    assert record is not None
    assert record.voltage_v == 120.0
    assert record.current_a == 5.0
    assert record.power_factor is None
    # A different reader's values do not leak.
    assert store.latest("r2") is None
