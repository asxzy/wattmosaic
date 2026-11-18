"""AT-07: a non-identifiable couple produces a group estimate, no split (HB §8.4).

Closing task: I4.  This file was created by I4 as the acceptance test for the
mandatory 250 W scenario; the K07 harness directory does not exist in this wave,
so the scenario builders are shared with ``tests/unit/test_group_250w.py``
(I4's own named test) instead of importing a not-yet-landed X07 fixture.

The assertions here are on the **contract objects** themselves
(``DeviceGroupEstimate``/``DeviceEstimate``), never on HTTP status or on the
test's own normalisation, so a regression in assembly cannot be hidden by the
adapter.  Variants A/B/C and the three mutation guards are included, as AT-07
requires.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest
from wattmosaic_engine.contracts import Bounds, IdentifiabilityStatus
from wattmosaic_engine.inference import bounds as bounds_module
from wattmosaic_engine.inference import identifiability as identifiability_module

from tests.unit import test_group_250w as scenario

GROUP_MEMBERS = scenario.GROUP_MEMBERS
READERS = scenario.READERS
T0 = scenario.T0
CADENCE_S = scenario.CADENCE_S


def _readings():
    return {"Circuit A": 250.0, "Subpanel": 249.7, "Main": 250.4}


def _assert_contract_level(published) -> None:
    """The contract half: one group, identified sum, null members, honest bounds."""
    assert len(published.groups) == 1
    group = published.groups[0]
    assert group.identifiability == IdentifiabilityStatus.GROUP_IDENTIFIABLE.value
    assert group.point_w is not None and 250.0 - 3.0 <= group.point_w <= 250.0 + 3.0
    assert tuple(group.member_ids) == ("L1", "L2")
    assert group.group_id == published.group_ids["L1"] == published.group_ids["L2"]
    assert tuple(group.source_reader_ids) == tuple(READERS)
    kinds = {kind for kind, _id in group.evidence_kinds_and_ids}
    assert "event_group" in kinds
    assert "observation" in kinds

    for member in GROUP_MEMBERS:
        assert published.points[member] is None
        assert (
            published.identifiability[member]
            == IdentifiabilityStatus.NON_IDENTIFIABLE.value
        )
        assert published.statuses[member] == "unknown"
        lower, upper = published.bounds[member]
        assert lower is not None and upper is not None
        assert lower == pytest.approx(0.0, abs=3.0)
        assert upper == pytest.approx(250.0, abs=3.0)
        assert published.basis[member] == "non_negativity"
        assert published.points[member] != pytest.approx(125.0)

    assert published.unknown_total_w == pytest.approx(0.0, abs=3.0)
    assert published.attributed_total_w == pytest.approx(250.0, abs=3.0)


@pytest.mark.parametrize(
    ("label", "models", "at_offset"),
    [
        ("A transition, no models", None, 0.0),
        ("B models declare 125 W each", {"L1": 125.0, "L2": 125.0}, 0.0),
        ("C steady state one cycle later", {"L1": 125.0, "L2": 125.0}, CADENCE_S),
    ],
)
def test_at07_group_estimate_not_a_fabricated_split(label, models, at_offset):
    published = scenario.run_new_pipeline(
        readings=_readings(),
        history=scenario._history(),
        models=models,
        at=T0 + timedelta(seconds=at_offset),
    )
    _assert_contract_level(published)


def test_at07_a_mutation_that_splits_equally_fails(monkeypatch):
    """Mutation guard 1: the solver patched to return the equal split."""
    real_solve = scenario._solve

    def equal_split(*args, **kwargs):
        solution = real_solve(*args, **kwargs)
        return scenario.JointSolution(
            x=np.full_like(solution.x, 125.0),
            cov=None,
            u=solution.u,
            active_rows=solution.active_rows,
            row_residual_w=solution.row_residual_w,
            objective=solution.objective,
            converged=solution.converged,
            mode=solution.mode,
        )

    scenario._force_identifiable(monkeypatch)
    monkeypatch.setattr(scenario, "_solve", equal_split)
    mutated = scenario.run_new_pipeline(
        readings=_readings(),
        history=scenario._history(),
    )
    with pytest.raises(AssertionError):
        _assert_contract_level(mutated)


def test_at07_a_mutation_that_fills_from_models_fails(monkeypatch):
    """Mutation guard 2: estimate assembly patched to fill from model priors."""
    scenario._force_identifiable(monkeypatch)
    monkeypatch.setattr(
        identifiability_module,
        "member_point_w",
        lambda *, solution_w, model_w, identifiability: model_w,
    )
    mutated = scenario.run_new_pipeline(
        readings=_readings(),
        history=scenario._history(),
        models={"L1": 125.0, "L2": 125.0},
    )
    with pytest.raises(AssertionError):
        _assert_contract_level(mutated)


def test_at07_a_mutation_that_collapses_the_bounds_fails(monkeypatch):
    """Mutation guard 3: bounds collapsed to ``[250, 250]``."""
    real = bounds_module.compute_bounds

    def collapsed(subject_ids, **kwargs):
        result = real(subject_ids, **kwargs)
        return {subject_id: Bounds._point(250.0, "group_sum") for subject_id in result}

    monkeypatch.setattr(
        "wattmosaic_engine.inference.identifiability.compute_bounds", collapsed
    )
    mutated = scenario.run_new_pipeline(
        readings=_readings(), history=scenario._history()
    )
    with pytest.raises(AssertionError):
        _assert_contract_level(mutated)
