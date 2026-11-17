"""Coverage and determinism gate for the canonical T01–T15 scenario builders.

``REQ-TEST-003``/``REQ-TEST-004`` and the §4.1 catalogue in
``01-requirements.md`` require a named deterministic builder for every declared
scenario id.  These tests fail if an id is declared without a builder, if a
builder is registered for an undeclared id, or if two builds of any id disagree.
"""

from __future__ import annotations

import pytest

from tools.simulator.scenarios import SCENARIO_BUILDERS, SCENARIO_IDS


def test_every_declared_scenario_id_has_a_builder() -> None:
    missing = [sid for sid in SCENARIO_IDS if sid not in SCENARIO_BUILDERS]
    assert not missing, f"declared scenario ids without a builder: {missing}"


def test_every_builder_is_for_a_declared_scenario_id() -> None:
    undeclared = sorted(set(SCENARIO_BUILDERS) - set(SCENARIO_IDS))
    assert not undeclared, f"builders for undeclared scenario ids: {undeclared}"


def test_declared_and_built_id_sets_are_equal() -> None:
    assert set(SCENARIO_IDS) == set(SCENARIO_BUILDERS)


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_every_scenario_builder_is_deterministic(scenario_id: str) -> None:
    build = SCENARIO_BUILDERS[scenario_id]
    first = build(seed=2026, minutes=4, interval=30)
    second = build(seed=2026, minutes=4, interval=30)
    assert first.to_list() == second.to_list(), f"{scenario_id} is not deterministic"
    assert first.signature() == second.signature(), f"{scenario_id} signature differs"


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_every_scenario_builder_produces_records(scenario_id: str) -> None:
    house = SCENARIO_BUILDERS[scenario_id](seed=7, minutes=4, interval=30)
    assert house.to_list(), f"{scenario_id} produced no records"
