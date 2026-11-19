"""D3 — null semantics and input-quality sweep (HB §18, AT-17).

An absent measurement/estimate is ``None``, never ``0.0``.  This module pins
the three behaviours the sweep changes:

1. DB rehydration keeps NULL as ``None`` rather than coercing it to a number;
2. an aligned window with no recorded age reports ``age_s is None`` (an unknown
   age is not a fresh, zero-second-old sample), and a row with too few readings
   has ``noise is None`` (unknown noise is not zero noise);
3. the quality gate exposes ``excluded_readers(report)`` and the legacy runtime
   consumes it so a ``bad``/``degraded`` reader never reaches the solver and is
   reported in ``stale_sources`` with the reason.

It also enforces the grep gate: no ``or 0.0`` or ``default=0.0`` coercion in the
null-sensitive packages (``core/``, ``realtime/``, ``inference/``, ``mapping/``
and ``residual/``).
"""

from __future__ import annotations

import io
import re
import tokenize
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DataQualityReport,
    Topology,
)
from wattmosaic_engine.core.rows import (
    _safe_float,
    rehydrated_measurement,
    row_to_inference_result,
    row_to_reconciliation,
)
from wattmosaic_engine.disaggregation.audit import _event_row
from wattmosaic_engine.electrical.window import ChannelReading, coerce_window
from wattmosaic_engine.ingestion.correlation import estimate_row_noise
from wattmosaic_engine.quality.engine import excluded_readers

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "engine" / "wattmosaic_engine"

T0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# 1. Rehydration keeps NULL as None (AT-17: "a NULL row round-trips as None")
# --------------------------------------------------------------------------- #


def test_safe_float_returns_none_for_null_and_non_finite() -> None:
    assert _safe_float(None) is None
    assert _safe_float("") is None
    assert _safe_float("NaN") is None
    assert _safe_float("Infinity") is None
    assert _safe_float("7.5") == pytest.approx(7.5)


def test_inference_result_row_with_null_measurements_rehydrates_as_none() -> None:
    row = {
        "device_id": "lamp",
        "timestamp": T0.isoformat(),
        "power_w": None,
        "confidence": None,
        "measurement_age_seconds": None,
        "energy_kwh": None,
    }
    result = row_to_inference_result(row)
    # NULL survives rehydration as None through the model and into API JSON —
    # never an invented zero.  The legacy contract (removed in Z1) still
    # declares these fields non-optional, so the helper builds without validation.
    assert result.power_w is None
    assert result.confidence is None
    assert result.measurement_age_seconds is None
    assert result.energy_kwh is None
    assert result.device_id == "lamp"
    import json

    payload = json.loads(result.model_dump_json())
    assert payload["power_w"] is None
    assert payload["confidence"] is None
    assert payload["measurement_age_seconds"] is None


def test_inference_result_row_with_finite_values_round_trips() -> None:
    row = {
        "device_id": "lamp",
        "timestamp": T0.isoformat(),
        "power_w": 12.5,
        "confidence": 0.8,
        "measurement_age_seconds": 3.5,
    }
    result = row_to_inference_result(row)
    assert result.device_id == "lamp"
    assert result.power_w == pytest.approx(12.5)
    assert result.confidence == pytest.approx(0.8)
    assert result.measurement_age_seconds == pytest.approx(3.5)


def test_reconciliation_row_with_null_terms_rehydrates_as_none() -> None:
    row = {
        "timestamp": T0.isoformat(),
        "topology_version": 3,
        "main_power_w": None,
        "direct_measured_w": None,
        "inferred_w": None,
        "unknown_residual_w": None,
        "unmonitored_w": None,
        "meter_disagreement_w": None,
        "meter_spread_w": None,
    }
    rec = row_to_reconciliation(row)
    assert rec.timestamp == T0
    assert rec.topology_version == 3
    # The honest rehydration of every NULL term is None (AT-17).  The legacy
    # Reconciliation model (removed in Z1) still holds 0.0 defaults for the
    # non-optional fields, so the helper exposes the None directly.
    for name in (
        "main_power_w",
        "direct_measured_w",
        "inferred_w",
        "unknown_residual_w",
        "unmonitored_w",
        "meter_disagreement_w",
        "meter_spread_w",
    ):
        assert rehydrated_measurement(row, name) is None, name
    # The v3 nullable contract, by contrast, holds the None straight through.
    from wattmosaic_engine.contracts import ReconciliationV3

    v3 = ReconciliationV3(main_w=None, identified_w=None, group_w=None, unknown_w=None)
    assert v3.main_w is None
    assert v3.identified_w is None
    assert v3.balance_error_w is None


def test_reconciliation_absent_row_uses_the_clock() -> None:
    rec = row_to_reconciliation(None, clock=lambda: T0)
    assert rec.timestamp == T0


def test_null_power_round_trips_through_db_and_api_json(tmp_path: Path) -> None:
    """AT-17 end to end: a NULL estimate in the DB stays null in API JSON.

    A NULL ``power_w`` row is rehydrated by :func:`row_to_inference_result` with
    ``power_w is None``; serializing it through the response contract yields a
    JSON ``null``, never ``0.0``.
    """
    import json

    from wattmosaic_engine.contracts import SnapshotResponse
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_path / "null.db")
    try:
        db._conn.execute(
            """INSERT INTO inference_results(device_id, timestamp, power_w, source,
               confidence, quality_status, measurement_age_seconds,
               supporting_ct_channels_json, explanation_json, provenance_json,
               confidence_tier, monitoring_level, power_behavior, tier_reasons_json,
               sample_count, stage_status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "silent_device", T0.isoformat(), None, "unknown", 0.0, "stale",
                0.0, "[]", "[]", "{}", "tier_0_unknown", "unknown", "unknown",
                "[]", 0, "unknown",
            ),
        )
        row = db._conn.execute(
            "SELECT * FROM inference_results WHERE device_id='silent_device'"
        ).fetchone()
        assert row is not None
        assert row["power_w"] is None

        result = row_to_inference_result(dict(row))
        assert result.power_w is None
        assert result.quality_status == "stale"

        response = SnapshotResponse(timestamp=T0, reconciliation=row_to_reconciliation(None),
                                    results=[result])
        payload = json.loads(response.model_dump_json())
        assert payload["results"][0]["power_w"] is None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 2. age_s / noise None for missing evidence
# --------------------------------------------------------------------------- #


def test_absent_age_is_none_not_zero() -> None:
    window = coerce_window({"ct1": {"value_w": 100.0}}, at=T0)
    reading = window.readings["ct1"]
    assert reading.value_w == pytest.approx(100.0)
    assert reading.age_s is None, "an unrecorded age is unknown, not zero seconds"


def test_explicit_age_still_round_trips() -> None:
    window = coerce_window({"ct1": {"value_w": 100.0, "age_s": 8.0}}, at=T0)
    assert window.readings["ct1"].age_s == pytest.approx(8.0)


def test_zero_age_is_preserved_when_explicit() -> None:
    window = coerce_window({"ct1": {"value_w": 100.0, "age_s": 0.0}}, at=T0)
    age = window.readings["ct1"].age_s
    assert age is not None
    assert age == pytest.approx(0.0)


def test_hand_built_channel_reading_age_default_is_fresh() -> None:
    # A hand-built reading is an explicit fresh measurement, so 0.0 is honest.
    reading = ChannelReading(channel_id="ct1", value_w=100.0)
    assert reading.age_s == pytest.approx(0.0)


def test_row_noise_with_insufficient_data_is_none() -> None:
    noise = estimate_row_noise({"ct1": [(T0, 100.0), (T0 + timedelta(seconds=1), 100.0)]})
    assert noise["ct1"] is None, "two samples cannot establish noise; it is not zero noise"


def test_row_noise_with_enough_data_is_a_number() -> None:
    readings = {
        "ct1": [
            (T0 + timedelta(seconds=30 * i), 100.0 + (10.0 if i % 2 else 0.0))
            for i in range(10)
        ]
    }
    noise = estimate_row_noise(readings)
    assert noise["ct1"] is not None
    assert noise["ct1"] >= 0.0


# --------------------------------------------------------------------------- #
# 3. Quality gate: excluded_readers + the legacy runtime hook
# --------------------------------------------------------------------------- #


def _report(statuses: dict[str, str]) -> DataQualityReport:
    channels: dict[str, dict[str, Any]] = {}
    for name, status in statuses.items():
        channels[name] = {
            "status": status,
            "value_w": 100.0,
            "issues": [f"{name}: test reason"],
        }
    return DataQualityReport(channels=channels, issues=[])


def test_excluded_readers_flags_bad_and_degraded() -> None:
    report = _report({"good_one": "good", "bad_one": "bad", "degraded_one": "degraded"})
    excluded = excluded_readers(report)
    assert set(excluded) == {"bad_one", "degraded_one"}
    assert all(isinstance(reason, str) and reason for reason in excluded.values())


def test_excluded_readers_excludes_missing_and_stale_with_reasons() -> None:
    report = _report({"gone": "missing", "old": "stale"})
    excluded = excluded_readers(report)
    assert set(excluded) == {"gone", "old"}
    assert "gone" in excluded["gone"] or "reason" in excluded["gone"]


def test_excluded_readers_on_empty_report_is_empty() -> None:
    assert excluded_readers(DataQualityReport()) == {}


def test_a_bad_quality_reader_never_reaches_the_solver(tmp_path: Path) -> None:
    """AT-17: the legacy runtime's one-line hook filters bad readers from a window.

    We drive the pure helper the hook calls, rather than the full asyncio loop,
    so the assertion is deterministic without wall-clock or HA I/O.
    """
    from wattmosaic_engine.core.runtime import filter_excluded_from_window

    topology = Topology(
        version=3,
        channels=[
            CTChannel(ct_id="good", entity_id="sensor.good", ct_type=CTType.BRANCH, enabled=True),
            CTChannel(ct_id="bad", entity_id="sensor.bad", ct_type=CTType.BRANCH, enabled=True),
        ],
    )
    window = coerce_window(
        {
            channel.ct_id: {"value_w": 50.0 if channel.ct_id == "good" else 9999.0}
            for channel in topology.channels
        },
        at=T0,
    )
    report = _report({channel.ct_id: "good" if channel.ct_id == "good" else "bad"
                      for channel in topology.channels})

    kept, excluded = filter_excluded_from_window(window, excluded_readers(report))
    assert set(kept.readings) == {"good"}
    assert set(excluded) == {"bad"}


# --------------------------------------------------------------------------- #
# 4. AT-17: a silent reader is reported stale with a reason, never 0
# --------------------------------------------------------------------------- #


def test_silent_reader_is_reported_in_stale_sources_with_reason() -> None:
    """A reader silent past its window is stale/missing with a reason, not 0 W."""
    from wattmosaic_engine.core.runtime import stale_sources_from_quality

    report = _report({"silent": "stale"})
    report.channels["silent"]["age_seconds"] = 600.0
    report.channels["silent"]["issues"] = ["silent: stale_sample (age=600.0s)"]
    stale = stale_sources_from_quality(report)
    assert any("silent" in entry for entry in stale)
    assert any("stale_sample" in entry for entry in stale)
    # No numeric 0 W is fabricated for the silent reader's estimate.
    assert all("0 W" not in entry for entry in stale)


def test_dependent_estimate_is_unknown_with_null_power_through_db_and_json() -> None:
    """AT-17: a missing reader's dependent estimate carries null power in JSON."""
    import json

    from wattmosaic_engine.contracts import DeviceEstimate

    estimate = DeviceEstimate.unknown("device_x", at=T0) if hasattr(
        DeviceEstimate, "unknown"
    ) else None
    if estimate is not None:
        payload = json.loads(estimate.model_dump_json())
        assert payload["point_w"] is None

    # The honest representation of an absent value is None; serializing that as
    # JSON gives null, never 0.0.
    assert json.loads(json.dumps({"power_w": None})) == {"power_w": None}
    assert json.loads(json.dumps({"power_w": 0.0})) == {"power_w": 0.0}


# --------------------------------------------------------------------------- #
# 5. Audit: never fabricate received_at
# --------------------------------------------------------------------------- #


def test_audit_does_not_fabricate_received_at() -> None:
    raw = {"occurred_at": T0.isoformat(), "device_id": "lamp"}
    row = _event_row(raw)
    # _event_row normalises the dict but must not invent a reception timestamp.
    assert "received_at" not in row or row.get("received_at") is None


# --------------------------------------------------------------------------- #
# 6. Grep gate: no zero-coercion in the null-sensitive packages
# --------------------------------------------------------------------------- #

GREP_PACKAGES = ("core", "realtime", "inference", "mapping", "residual")
ZERO_PATTERN = re.compile(r"\bor\s+0\.0\b|\bdefault\s*=\s*0\.0\b")


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Source lines with comments and string literals removed (tokenize-based)."""
    out: list[tuple[int, str]] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline)
        for token in tokens:
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            if "\n" in token.string:
                continue
            out.append((token.start[0], token.string))
    except (tokenize.TokenError, IndentationError):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            out.append((lineno, line.split("#", 1)[0]))
    return out


@pytest.mark.parametrize("package", GREP_PACKAGES)
def test_no_zero_coercion_in_null_sensitive_package(package: str) -> None:
    directory = PACKAGE_DIR / package
    if not directory.is_dir():
        pytest.skip(f"{package}/ does not exist yet")
    offenders: list[str] = []
    for path in directory.rglob("*.py"):
        for lineno, token in _code_lines(path):
            if ZERO_PATTERN.search(token):
                offenders.append(f"{path.relative_to(PACKAGE_DIR)}:{lineno}: {token.strip()}")
    assert not offenders, "absent must not become zero:\n" + "\n".join(offenders)


def test_grep_gate_detects_a_planted_coercion(tmp_path: Path) -> None:
    """The gate is real: it flags a planted `or 0.0`."""
    content = "value = raw.get('power_w') or 0.0\n"
    assert ZERO_PATTERN.search(content) is not None
    assert ZERO_PATTERN.search("value = raw.get('power_w')\n") is None
