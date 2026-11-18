"""AT-07 — non-identifiable → group estimate, no fabricated individual values.

Closes: I4. The mandatory 250 W test (tasks/I4.md), with all three variants
(A: transition, B: model-prior, C: steady state) and the three mutation guards,
plus the solo-switch positive control. Two always-co-switched lights on
Circuit A produce one GROUP_IDENTIFIABLE device group of 250 W; each member has
``point_w is None``, NON_IDENTIFIABLE, justified non-negativity bounds [0, 250]
± noise. Model priors never become bounds and no 125 W equal split appears
anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.xfail(strict=True, reason="closed by I4"),
]


def _versions():
    from wattmosaic_engine.contracts import VersionTriple

    return VersionTriple(
        topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
    )


def _history(subject_ids, *, solo=False):
    """12 co-switch groups (I4 setup); optional 3 L1 solo groups (positive control)."""
    from wattmosaic_engine.runtime.fakes import InMemoryHistory
    from wattmosaic_engine.runtime.interfaces import HistoryRow

    rows = []
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for k in range(12):
        rows.append(
            HistoryRow(
                group_id=f"g{k}",
                at=base + timedelta(minutes=k),
                subject_ids=subject_ids,
                reader_ids=("ct_a", "ct_sub", "ct_main"),
                delta_w_by_reader={"ct_a": 250.0, "ct_sub": 250.0, "ct_main": 250.0},
            )
        )
    if solo:
        for k in range(3):
            rows.append(
                HistoryRow(
                    group_id=f"solo{k}",
                    at=base + timedelta(minutes=100 + k),
                    subject_ids=("light_1",),
                    reader_ids=("ct_a", "ct_sub", "ct_main"),
                    delta_w_by_reader={"ct_a": 100.0},
                )
            )
    return InMemoryHistory(rows)


def _assert_non_identifiable_semantics(result) -> None:
    """The identical A/B/C member semantics + group assertions (I4 §Assertions)."""
    from wattmosaic_engine.contracts import BoundsBasis, EstimateStatus, IdentifiabilityStatus

    groups = [g for g in result.group_estimates if set(g.member_ids) == {"light_1", "light_2"}]
    assert len(groups) == 1, "exactly one DeviceGroupEstimate for the co-switched pair"
    group = groups[0]
    assert group.identifiability == IdentifiabilityStatus.GROUP_IDENTIFIABLE
    assert group.point_w == pytest.approx(250.0, abs=2.0)
    assert set(group.source_reader_ids) == {"ct_a", "ct_sub", "ct_main"}
    kinds = {r.kind for r in group.evidence_refs}
    assert "event_group" in kinds and "sample" in kinds

    for member in result.device_estimates:
        if member.subject_id not in ("light_1", "light_2"):
            continue
        assert member.point_w is None, "a non-identifiable member must have no point value"
        assert member.identifiability == IdentifiabilityStatus.NON_IDENTIFIABLE
        assert member.status not in (EstimateStatus.ESTIMATED, EstimateStatus.ALLOCATED)
        assert member.bounds.lower_w == pytest.approx(0.0, abs=2.0)
        assert member.bounds.upper_w == pytest.approx(250.0, abs=2.0)
        assert member.bounds.basis == BoundsBasis.NON_NEGATIVITY.value
        assert member.point_w != 125.0
        assert member.bounds.lower_w != member.bounds.upper_w

    assert result.residual_w_by_reader.get("ct_a", 0.0) == pytest.approx(0.0, abs=2.0)
    total = sum(e.point_w or 0.0 for e in result.device_estimates)
    assert total + sum(g.point_w or 0.0 for g in result.group_estimates) == pytest.approx(
        250.0, abs=2.0
    )


def _run_variant(kind, *, solo=False):
    """Run the 250 W scenario through the target infer_group surface (I4)."""
    from wattmosaic_engine.inference import infer_group  # target surface (I4)
    from wattmosaic_engine.runtime.fakes import (
        FakeMapping,
        FakeModelSet,
        FakeResidual,
        FakeTopology,
        InMemoryMatchStore,
        fake_operator,
    )

    from tests.acceptance.test_at07_group_not_fabricated import _history, _versions

    topo = FakeTopology.from_tree({"Main": {"Subpanel": {"Circuit A": {}}}})
    mapping_pairs = {
        ("device", "light_1"): "Circuit A",
        ("device", "light_2"): "Circuit A",
    }
    mapping = FakeMapping.from_pairs(mapping_pairs)
    operator = fake_operator(
        topo, mapping, ["Main", "Subpanel", "Circuit A"], list(mapping_pairs)
    )
    models = FakeModelSet.fixed({"light_1": 125.0, "light_2": 125.0})

    def clock() -> datetime:
        return datetime(2026, 1, 1, 1, 0, tzinfo=UTC)

    group = type("Group", (), {"group_id": "g250", "reader_deltas": []})()
    return infer_group(
        group,
        operator,
        models,
        FakeResidual(),
        _history(("light_1", "light_2"), solo=solo),
        InMemoryMatchStore(),
        _versions(),
        clock,
        variant=kind,
    )


def test_at07_250w_group_not_fabricated() -> None:
    """All 250 W variants, mutation guards and positive control in one test."""
    from wattmosaic_engine.contracts import EstimateStatus, IdentifiabilityStatus

    # Variants A (transition), B (model priors) and C (steady state) must all
    # produce identical, non-fabricated member semantics.
    for kind in ("A", "B", "C"):
        _assert_non_identifiable_semantics(_run_variant(kind))

    # Mutation guards: each mutation of the pipeline must make the test fail.
    from wattmosaic_engine.inference import identifiability as ident

    with (
        patch.object(ident, "solve_equal_split", return_value=(125.0, 125.0)),
        pytest.raises(AssertionError),
    ):
        _assert_non_identifiable_semantics(_run_variant("A"))
    with (
        patch.object(ident, "assemble_members_from_models", return_value=True),
        pytest.raises(AssertionError),
    ):
        _assert_non_identifiable_semantics(_run_variant("A"))
    with (
        patch.object(ident, "collapse_bounds_to_point", return_value=True),
        pytest.raises(AssertionError),
    ):
        _assert_non_identifiable_semantics(_run_variant("A"))

    # Positive control: with 3 L1 solo groups the pair becomes identifiable.
    result = _run_variant("A", solo=True)
    group = next(g for g in result.group_estimates if set(g.member_ids) == {"light_1", "light_2"})
    assert group.identifiability == IdentifiabilityStatus.IDENTIFIABLE
    by_id = {e.subject_id: e for e in result.device_estimates}
    assert by_id["light_1"].point_w == pytest.approx(100.0, abs=5.0)
    assert by_id["light_2"].point_w == pytest.approx(150.0, abs=5.0)
    assert by_id["light_1"].status == EstimateStatus.ESTIMATED
