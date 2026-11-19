"""I4 identifiability: experiment matrix, states, groups and stable ids (HB §8.4).

The tests use K08's deterministic fakes for the runtime handoffs and build the
history explicitly, so nothing here depends on storage, a clock or a model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from wattmosaic_engine.contracts import (
    IdentifiabilityStatus,
    VersionTriple,
)
from wattmosaic_engine.inference.identifiability import (
    CORRELATION_TOL,
    NULL_SHARE_TOL,
    IdentifiabilityAnalyzer,
    assemble_estimates,
    member_point_w,
    subject_group_id,
)
from wattmosaic_engine.runtime.interfaces import HistoryRow, JointSolution

from tests.support.fakes import (
    InMemoryHistory,
)

T0 = datetime(2026, 11, 5, 18, 0, 0, tzinfo=UTC)
VERSIONS = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-test"
)


# --------------------------------------------------------------------------- #
# Builders.
# --------------------------------------------------------------------------- #
def _operator(rows, subjects, mapping, *, at=T0):
    """Build an operator with an explicit H plus the reader ids of each row.

    Constructed directly (rather than through ``fake_operator``) because these
    tests pin an exact H; the fakes still supply the topology-free pieces of the
    runtime surface used elsewhere in the suite.
    """
    from types import MappingProxyType

    from wattmosaic_engine.contracts import Observability
    from wattmosaic_engine.runtime.interfaces import EntryMeta, ObservationOperator, RowMeta

    del mapping
    readers = tuple(
        reader for _values, row_readers in rows for reader in row_readers
    )
    if len(readers) != len(rows):
        raise ValueError("each test row names exactly one reader")
    H = np.array([values for values, _readers in rows], dtype=float)
    entries = {
        (row, column): EntryMeta(
            coefficient=float(H[row, column]),
            observability=Observability.DIRECT if H[row, column] else Observability.UNKNOWN,
            edge_lifecycle=None,
            edge_ids=(),
        )
        for row in range(H.shape[0])
        for column in range(H.shape[1])
        if H[row, column]
    }
    return ObservationOperator(
        H=H,
        rows=tuple(
            RowMeta(
                reader_id=reader,
                kind="reader",
                sigma_w=None,
                sigma_inflation=1.0,
                excluded_reason=None,
            )
            for reader in readers
        ),
        subjects=tuple(subjects),
        entries=MappingProxyType(entries),
        unknown_scopes=(),
        soft_alternatives=(),
        topology_version_id="topo-1",
        mapping_version_id="map-1",
        at=at,
    )


def _solution(x, *, cov=None, active_rows=None):
    x = np.asarray(x, dtype=float)
    rows = active_rows if active_rows is not None else tuple(range(6))
    return JointSolution(
        x=x,
        cov=None if cov is None else np.asarray(cov, dtype=float),
        u=np.zeros(0),
        active_rows=tuple(rows),
        row_residual_w=np.zeros(len(rows)),
        objective=0.0,
        converged=True,
        mode="delta",
    )


def _together(history: InMemoryHistory, count: int, members=("L1", "L2"), reader="Circuit A"):
    for index in range(count):
        history.add(
            HistoryRow(
                group_id=f"together-{index:02d}",
                at=T0 - timedelta(hours=index + 1),
                subject_ids=tuple(members),
                reader_ids=(reader,),
                delta_w_by_reader={reader: 250.0},
            )
        )
    return history


NESTED = (
    ([1.0, 1.0], ("Circuit A",)),
    ([1.0, 1.0], ("Subpanel",)),
    ([1.0, 1.0], ("Main",)),
)
SUBJECTS = (("device", "L1"), ("device", "L2"))
MAPPING = {
    ("device", "L1"): "Circuit A",
    ("device", "L2"): "Circuit A",
}


# --------------------------------------------------------------------------- #
# The five states.
# --------------------------------------------------------------------------- #
def test_co_switched_pair_is_group_identifiable_with_a_published_sum():
    operator = _operator(NESTED, SUBJECTS, MAPPING)
    history = _together(InMemoryHistory(), 12)
    report = IdentifiabilityAnalyzer().analyze(
        operator, _solution([125.0, 125.0]), ["L1", "L2"], history
    )
    assert report.rank == 1
    assert report.n_experiments == 15  # 3 current rows + 12 history rows
    assert report.subjects == {
        "L1": IdentifiabilityStatus.NON_IDENTIFIABLE,
        "L2": IdentifiabilityStatus.NON_IDENTIFIABLE,
    }
    assert len(report.groups) == 1
    group = report.groups[0]
    # Pydantic stores the enum *value* (``use_enum_values``).
    assert group.identifiability == IdentifiabilityStatus.GROUP_IDENTIFIABLE.value
    assert group.sum_w == pytest.approx(250.0)
    assert group.source_reader_ids == ("Circuit A", "Subpanel", "Main")
    assert group.covers_all_candidates is True
    for share in report.null_space_share.values():
        assert share > NULL_SHARE_TOL


def test_independent_history_makes_the_pair_identifiable():
    """Two lights that switched independently are IDENTIFIABLE even when they
    co-switch now (C9: identifiability is H plus history, not this cycle)."""
    operator = _operator(NESTED, SUBJECTS, MAPPING)
    history = _together(InMemoryHistory(), 12)
    history.add(
        HistoryRow(
            group_id="solo-l1",
            at=T0 - timedelta(days=1),
            subject_ids=("L1",),
            reader_ids=("Circuit A",),
        )
    )
    report = IdentifiabilityAnalyzer().analyze(
        operator, _solution([100.0, 150.0]), ["L1", "L2"], history
    )
    assert report.rank == 2
    assert report.groups == ()
    assert report.subjects == {
        "L1": IdentifiabilityStatus.IDENTIFIABLE,
        "L2": IdentifiabilityStatus.IDENTIFIABLE,
    }
    for share in report.null_space_share.values():
        assert share <= NULL_SHARE_TOL


def test_three_devices_where_sum_and_one_member_are_identifiable_are_partial():
    """A is measured alone; B and C share a row. A is IDENTIFIABLE, B/C are
    PARTIALLY_IDENTIFIABLE and no group estimate is published for them."""
    subjects = (("device", "A"), ("device", "B"), ("device", "C"))
    rows = (
        ([1.0, 0.0, 0.0], ("Circuit A",)),
        ([0.0, 1.0, 1.0], ("Circuit B",)),
    )
    operator = _operator(
        rows,
        subjects,
        {("device", "A"): "Circuit A", ("device", "B"): "Circuit B", ("device", "C"): "Circuit B"},
    )
    history = InMemoryHistory(
        [
            HistoryRow(
                group_id=f"bc-{index}",
                at=T0 - timedelta(hours=index + 1),
                subject_ids=("B", "C"),
                reader_ids=("Circuit B",),
            )
            for index in range(4)
        ]
    )
    report = IdentifiabilityAnalyzer().analyze(
        operator, _solution([100.0, 80.0, 70.0], active_rows=(0, 1)), ["A", "B", "C"], history
    )
    assert report.subjects["A"] is IdentifiabilityStatus.IDENTIFIABLE
    assert report.subjects["B"] is IdentifiabilityStatus.PARTIALLY_IDENTIFIABLE
    assert report.subjects["C"] is IdentifiabilityStatus.PARTIALLY_IDENTIFIABLE
    assembly = assemble_estimates(
        report,
        operator,
        versions=VERSIONS,
        at=T0,
        points={"A": 100.0, "B": 80.0, "C": 70.0},
    )
    assert assembly.group_estimates == ()
    member = {estimate.subject_id: estimate for estimate in assembly.device_estimates}
    assert member["A"].point_w == pytest.approx(100.0)
    assert member["B"].point_w is None
    assert member["C"].point_w is None
    assert member["B"].group_id is None


def test_no_history_and_a_stale_reader_are_insufficient_evidence():
    """A usable row plus a stale sibling and no history: nothing can be said."""
    operator = _operator(NESTED, SUBJECTS, MAPPING)
    stale = tuple(
        row.__class__(**{**row.__dict__, "excluded_reason": "stale"})
        if row.reader_id == "Subpanel"
        else row
        for row in operator.rows
    )
    operator = type(operator)(
        H=operator.H,
        rows=stale,
        subjects=operator.subjects,
        entries=operator.entries,
        unknown_scopes=(),
        soft_alternatives=(),
        topology_version_id=operator.topology_version_id,
        mapping_version_id=operator.mapping_version_id,
        at=operator.at,
    )
    report = IdentifiabilityAnalyzer().analyze(
        operator,
        _solution([125.0, 125.0], active_rows=(0, 2)),
        ["L1", "L2"],
        InMemoryHistory(),
    )
    assert set(report.subjects.values()) == {IdentifiabilityStatus.INSUFFICIENT_EVIDENCE}
    assert report.usable_rows == (0, 2)
    assert report.groups == ()


def test_only_stale_rows_are_insufficient_evidence():
    """With every reader stale there is no usable row at all."""
    operator = _operator(NESTED, SUBJECTS, MAPPING)
    stale = tuple(
        row.__class__(**{**row.__dict__, "excluded_reason": "stale"}) for row in operator.rows
    )
    operator = type(operator)(
        H=operator.H,
        rows=stale,
        subjects=operator.subjects,
        entries=operator.entries,
        unknown_scopes=(),
        soft_alternatives=(),
        topology_version_id=operator.topology_version_id,
        mapping_version_id=operator.mapping_version_id,
        at=operator.at,
    )
    report = IdentifiabilityAnalyzer().analyze(
        operator,
        _solution([125.0, 125.0], active_rows=()),
        ["L1", "L2"],
        _together(InMemoryHistory(), 12),
    )
    assert set(report.subjects.values()) == {IdentifiabilityStatus.INSUFFICIENT_EVIDENCE}
    assert report.rank == 0


def test_one_historical_event_is_below_the_evidence_floor():
    operator = _operator(NESTED, SUBJECTS, MAPPING)
    report = IdentifiabilityAnalyzer().analyze(
        operator, _solution([125.0, 125.0]), ["L1", "L2"], _together(InMemoryHistory(), 1)
    )
    assert set(report.subjects.values()) == {IdentifiabilityStatus.INSUFFICIENT_EVIDENCE}


def test_a_posterior_wider_than_the_limit_is_not_called_identifiable():
    """A zero null-space share plus a large posterior sigma is not IDENTIFIABLE."""
    operator = _operator(
        (([1.0, 0.0], ("Circuit A",)), ([0.0, 1.0], ("Circuit B",))),
        SUBJECTS,
        {("device", "L1"): "Circuit A", ("device", "L2"): "Circuit B"},
    )
    cov = np.diag([400.0, 4.0])  # sqrt = 20 W > max(5 W, 10 % * 100 W)
    report = IdentifiabilityAnalyzer().analyze(
        operator, _solution([100.0, 100.0], cov=cov, active_rows=(0, 1)), ["L1", "L2"], InMemoryHistory()
    )
    assert report.subjects["L1"] is IdentifiabilityStatus.PARTIALLY_IDENTIFIABLE
    assert report.subjects["L2"] is IdentifiabilityStatus.IDENTIFIABLE


# --------------------------------------------------------------------------- #
# Matrix report contents.
# --------------------------------------------------------------------------- #
def test_report_publishes_rank_condition_number_and_correlation():
    operator = _operator(NESTED, SUBJECTS, MAPPING)
    report = IdentifiabilityAnalyzer().analyze(
        operator, _solution([125.0, 125.0]), ["L1", "L2"], _together(InMemoryHistory(), 12)
    )
    assert report.condition_number is not None
    assert report.singular_values[0] > 0.0
    assert report.column_correlation[("L1", "L2")] == pytest.approx(1.0)
    assert report.column_correlation[("L1", "L2")] > CORRELATION_TOL
    assert report.usable_rows == (0, 1, 2)


def test_a_missing_candidate_is_rejected_rather_than_invented():
    operator = _operator(NESTED, SUBJECTS, MAPPING)
    with pytest.raises(ValueError):
        IdentifiabilityAnalyzer().analyze(
            operator, _solution([125.0, 125.0]), ["L1", "L3"], InMemoryHistory()
        )
    with pytest.raises(ValueError):
        IdentifiabilityAnalyzer().analyze(
            operator, _solution([125.0, 125.0]), [], InMemoryHistory()
        )


def test_history_outside_the_window_is_ignored():
    operator = _operator(NESTED, SUBJECTS, MAPPING)
    old = InMemoryHistory(
        [
            HistoryRow(
                group_id=f"old-{index}",
                at=T0 - timedelta(days=120, hours=index),
                subject_ids=("L1", "L2"),
                reader_ids=("Circuit A",),
            )
            for index in range(12)
        ]
    )
    report = IdentifiabilityAnalyzer().analyze(
        operator, _solution([125.0, 125.0]), ["L1", "L2"], old
    )
    assert report.n_history_events == 0
    assert set(report.subjects.values()) == {IdentifiabilityStatus.INSUFFICIENT_EVIDENCE}


def test_history_is_capped():
    operator = _operator(NESTED, SUBJECTS, MAPPING)
    history = InMemoryHistory(
        [
            HistoryRow(
                group_id=f"many-{index:03d}",
                at=T0 - timedelta(minutes=index + 1),
                subject_ids=("L1", "L2"),
                reader_ids=("Circuit A",),
            )
            for index in range(40)
        ]
    )
    report = IdentifiabilityAnalyzer(max_history_events=5).analyze(
        operator, _solution([125.0, 125.0]), ["L1", "L2"], history
    )
    assert report.n_history_events == 5
    assert report.n_experiments == 8  # 3 current + 5 history rows


def test_analysis_is_deterministic():
    operator = _operator(NESTED, SUBJECTS, MAPPING)
    history = _together(InMemoryHistory(), 12)
    first = IdentifiabilityAnalyzer().analyze(
        operator, _solution([125.0, 125.0]), ["L1", "L2"], history
    )
    second = IdentifiabilityAnalyzer().analyze(
        operator, _solution([125.0, 125.0]), ["L2", "L1"], history
    )
    assert first.rank == second.rank
    assert first.groups == second.groups
    assert np.array_equal(first.experiment_matrix, second.experiment_matrix)


# --------------------------------------------------------------------------- #
# Group identity and assembly.
# --------------------------------------------------------------------------- #
def test_group_id_is_the_sorted_member_hash():
    import hashlib

    assert subject_group_id(("L2", "L1")) == subject_group_id(("L1", "L2"))
    expected = hashlib.sha256("\0".join(["L1", "L2"]).encode()).hexdigest()[:16]
    assert subject_group_id(("L1", "L2")) == expected
    assert subject_group_id(("L1", "L2", "L3")) != subject_group_id(("L1", "L2"))
    with pytest.raises(ValueError):
        subject_group_id(())


def test_member_point_is_none_unless_identifiable():
    assert (
        member_point_w(
            solution_w=125.0,
            model_w=125.0,
            identifiability=IdentifiabilityStatus.NON_IDENTIFIABLE,
        )
        is None
    )
    assert (
        member_point_w(
            solution_w=125.0,
            model_w=125.0,
            identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
        )
        is None
    )
    assert (
        member_point_w(
            solution_w=125.0,
            model_w=None,
            identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        )
        == 125.0
    )
    assert (
        member_point_w(
            solution_w=None,
            model_w=90.0,
            identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        )
        == 90.0
    )
    assert (
        member_point_w(
            solution_w=None,
            model_w=None,
            identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        )
        is None
    )


def test_assembly_publishes_one_group_and_null_members():
    operator = _operator(NESTED, SUBJECTS, MAPPING)
    report = IdentifiabilityAnalyzer().analyze(
        operator, _solution([125.0, 125.0]), ["L1", "L2"], _together(InMemoryHistory(), 12)
    )
    assembly = assemble_estimates(
        report,
        operator,
        versions=VERSIONS,
        at=T0,
        points={"L1": 125.0, "L2": 125.0},
        models={"L1": 125.0, "L2": 125.0},
    )
    assert len(assembly.group_estimates) == 1
    group = assembly.group_estimates[0]
    assert group.point_w == pytest.approx(250.0)
    assert group.member_ids == ["L1", "L2"]
    # Pydantic stores the enum *value* (``use_enum_values``).
    assert group.identifiability == IdentifiabilityStatus.GROUP_IDENTIFIABLE.value
    assert {ref.kind for ref in group.evidence_refs} == {"event_group", "observation"}
    for estimate in assembly.device_estimates:
        assert estimate.point_w is None
        assert estimate.identifiability == IdentifiabilityStatus.NON_IDENTIFIABLE.value
        assert estimate.group_id == group.group_id
        assert estimate.bounds.basis == "non_negativity"
        assert (estimate.bounds.lower_w, estimate.bounds.upper_w) == pytest.approx(
            (0.0, 250.0)
        )


def test_assembly_publishes_nothing_without_history():
    operator = _operator(NESTED, SUBJECTS, MAPPING)
    report = IdentifiabilityAnalyzer().analyze(
        operator, _solution([125.0, 125.0]), ["L1", "L2"], InMemoryHistory()
    )
    assembly = assemble_estimates(
        report, operator, versions=VERSIONS, at=T0, points={"L1": 125.0, "L2": 125.0}
    )
    assert assembly.group_estimates == ()
    for estimate in assembly.device_estimates:
        assert estimate.point_w is None
        assert estimate.group_id is None
