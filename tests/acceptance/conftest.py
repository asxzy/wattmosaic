"""Fixtures for the HB §21 acceptance harness (K07).

The harness gives every acceptance test the same two things:

* a deterministic **HB §17 reference house** (``hb17``), built from the X07
  simulator via :func:`tools.simulator.hb17.build`, whose observation streams
  and oracle are kept strictly separate (``observation_view()`` vs ``truth``);
* thin **public-surface adapters** — ``engine_harness`` (the live runtime over
  the HB §17 replay, seeded by ``tests/support/hb17_seed``) and ``panel_model``
  — that construct the v3 surfaces the acceptance tests assert against.

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
def hb17_relocated():
    """The same seed-17 house in its ``relocated_tv`` variant (HB §17 steps 9-10).

    After step 9 the TV toggles on Circuit C, so the post-move physics reaches a
    reader the step-9 relocation moved it to; a 15-minute quiet prelude lets the
    P3 detector trust its noise estimate (HB §17 step 1).  Same shape as
    ``hb17``; observation and truth stay separate.
    """
    from tools.simulator.hb17 import build

    house = build(17, variant="relocated_tv", minutes=60, lead_in_minutes=15)
    return {
        "house": house,
        "observation": house.observation_view(),
        "truth": house.truth,
        "ha_messages": house.ha_messages,
        "reader_samples": house.reader_samples,
    }


@pytest.fixture()
def engine_harness() -> Callable[..., Any]:
    """Factory: a real ``EngineCore`` driven through the HB §17 replay.

    ``harness(hb17, devices=..., configure=..., run=...)`` seeds the user's
    declared topology/mapping through ``tests/support/hb17_seed`` (the engine's
    ``EngineHarness`` takes the seed as a callable and imports no scenario
    data), then replays the observation stream through the runtime's three
    loops.  The caller closes the returned harness.
    """
    from tests.support.hb17_seed import start_hb17_harness

    return start_hb17_harness


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
