"""Regression: the device history read path returned ``[]`` when rows existed.

``EngineCore._device_history`` passed ``since=utcnow()`` to
``Database.inference_history``, whose SQL is ``WHERE timestamp >= ?``.  Every
stored row has a ``timestamp`` in the past, so the predicate matched nothing and
``GET /api/v1/devices/{device_id}`` advertised an empty history forever
(``docs/assessment_gap_analysis.md`` G17).

At the 3.0 cutover ``GET /devices/{device_id}`` serves a v3 ``DeviceEstimate``
with no ``history`` field; revision history is now read from
``GET /devices/{device_id}/records``.  The three HTTP-payload tests that pinned
the v2 ``history`` field were removed with that shape; the engine-side lookback
config assertion below survives.
"""

from __future__ import annotations

import pytest


def test_history_lookback_default_is_one_day() -> None:
    from wattmosaic_engine.config.settings import EngineConfig

    assert EngineConfig().history_lookback_s == pytest.approx(86400.0)
