"""AT-17 — missing data does not become zero (HB §21).

Closes: D3. A reader silent past ``missing_after_s`` becomes None/stale with a
reason in the column; dependent estimates are UNKNOWN with null power through
DB, API JSON and HA state.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by D3"),
]


def test_missing_reader_is_none_through_every_layer(hb17) -> None:
    """An absent reading stays null everywhere; no layer fabricates 0 W."""
    from wattmosaic_engine.contracts import EstimateStatus

    result = run_missing_data_pipeline(hb17["observation"], silent_reader="ct_b")  # noqa: F821

    column = result.matrix.reader_totals["ct_b"]
    assert column.measured_w is None
    assert result.matrix.stale_sources  # a reason is recorded somewhere in payload
    assert "ct_b" in result.matrix_stale_reasons

    for est in result.snapshot.device_estimates:
        if "ct_b" in est.source_reader_ids:
            assert est.point_w is None
            assert est.status == EstimateStatus.UNKNOWN

    assert result.api_json["reader_totals"]["ct_b"]["measured_w"] is None
    assert result.ha_state["sensor.fridge_power"]["state"] == "unknown"
