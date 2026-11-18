"""Fixtures for the HB §21 acceptance harness (K07).

The harness gives every acceptance test the same two things:

* a deterministic **HB §17 reference house** (``hb17``), built from the X07
  simulator via :func:`tools.simulator.hb17.build`, whose observation streams
  and oracle are kept strictly separate (``observation_view()`` vs ``truth``);
* thin **public-surface adapters** — ``engine_harness``, ``api_client`` and
  ``panel_model`` — that construct the target v3 surfaces the acceptance tests
  assert against.  Until those surfaces land (P6, M3, U1/U2) the adapters raise
  ``NotImplementedError`` inside the fixture; because every acceptance test is
  ``xfail(strict=True)``, that is a clean xfail, not a collection error.

Fixture failures are treated as xfail (not error) by the ``xfail‐strict`` policy
of this suite: a fixture that raises ``NotImplementedError`` makes the test that
requests it xfail, which is exactly the intended state for an unclosed test.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``acceptance`` marker so ``pytest -q`` collects cleanly."""
    config.addinivalue_line("markers", "acceptance: HB §21 mandatory acceptance test")


@pytest.fixture()
def hb17():
    """The HB §17 reference house, seed 17 (X07), separated observation/truth.

    Returns a two-tuple ``(observation, truth)`` where ``observation`` contains
    only what an algorithm may see (HA messages and reader samples) and ``truth``
    is the oracle.  Keeping them as separate keys stops an acceptance test from
    accidentally asserting against ground truth it should have inferred.
    """
    from tools.simulator.hb17 import build

    house = build(17)
    assert house.ha_messages, "hb17 house produced no HA messages"
    assert house.reader_samples, "hb17 house produced no reader samples"
    return {
        "house": house,
        "observation": house.observation_view(),
        "truth": house.truth,
        "ha_messages": house.ha_messages,
        "reader_samples": house.reader_samples,
    }


@pytest.fixture()
def engine_harness() -> Callable[..., Any]:
    """Factory: start ``EngineCore`` with an injected clock + X07 replay driver.

    The returned callable, once P6 lands, is invoked as ``harness(hb17)`` and
    returns a running harness whose ``core`` speaks the v3 runtime interfaces and
    whose ``result`` is a :class:`JointInferenceResult`.  Before P6 exists this
    raises ``NotImplementedError`` so the acceptance tests that request it xfail
    strictly rather than erroring at collection time.
    """

    def _start(*args: Any, **kwargs: Any) -> Any:
        from wattmosaic_engine.realtime import EngineHarness  # noqa: F401

        return EngineHarness.from_hb17(*args, **kwargs)

    return _start


@pytest.fixture()
def api_client() -> Callable[..., Any]:
    """Factory: an aiohttp test client bound to the v3 routes under ``/api/v1``.

    Once M3 lands, ``client(hb17)`` yields an object with an async ``get(path)``
    returning decoded JSON.  Until then it raises ``NotImplementedError`` and
    the acceptance tests that request it xfail strictly.
    """

    def _client(*args: Any, **kwargs: Any) -> Any:
        from wattmosaic_engine.api.v3 import AcceptanceClient  # noqa: F401

        return AcceptanceClient.from_hb17(*args, **kwargs)

    return _client


@pytest.fixture()
def panel_model() -> Callable[..., Any]:
    """Factory: U1's ``matrix-model.js`` driven through ``panel_harness``.

    Once U1/U2 land, ``model(matrix_payload)`` feeds a :class:`MatrixPayload`
    into the real panel ES module via ``tests/unit/panel_harness.py`` and returns
    the rendered model object.  Until then it raises ``NotImplementedError`` so
    UI acceptance tests xfail strictly.
    """

    def _model(*args: Any, **kwargs: Any) -> Any:
        from tests.unit.panel_harness import run_esm  # noqa: F401

        raise NotImplementedError(
            "matrix-model.js v3 panel adapter is closed by U1/U2 (see K07 README)"
        )

    return _model
