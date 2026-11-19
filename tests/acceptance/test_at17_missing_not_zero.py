"""AT-17 — missing data does not become zero (HB §21).

Closes: D3. A reader silent past ``missing_after_s`` becomes None/stale with a
reason in the column; dependent estimates are UNKNOWN with null power through
DB, API JSON and HA state.
"""

from __future__ import annotations

import pytest

CLOSED_BY = "P6"

pytestmark = [pytest.mark.acceptance]


def test_missing_reader_is_none_through_every_layer(hb17) -> None:
    """An absent reading stays null everywhere; no layer fabricates 0 W."""
    from wattmosaic_engine.contracts import EstimateStatus

    from tests.support.missing_data import run_missing_reader

    # The live runtime replays the house; Circuit B stops reporting at 15 min
    # and the stream runs on past ``missing_after_s``.  Declared up front: a
    # fridge measured by Circuit B (a single-subject scope, DEC-05) and the TV
    # on its plug.
    result = run_missing_reader(
        hb17,
        silent_reader="ct_b",
        silent_from_s=900.0,
        devices={"tv": "ct_plug", "fridge": "ct_b"},
        single_subject={"ct_plug": "tv", "ct_b": "fridge"},
    )
    # Non-vacuous: some published estimate does depend on the silent reader.
    assert any("ct_b" in est.source_reader_ids for est in result.snapshot.device_estimates)

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
