"""The mandatory 250 W device-group test (I4, HP §8.4, AT-07, DEF-04).

Written **before** any solver code, as the task file requires: it fails on the
legacy ``DisaggregationEngine.infer`` path, which is why the adapter below can
run either the legacy engine or the new I4 pipeline over the *same* scenario.

House (analysis D §4a)::

    Main ⊃ Subpanel ⊃ Circuit A          Light 1, Light 2 on Circuit A only

History: 12 event groups in which L1 and L2 always switch within 1 s, so no
historical event ever separated them.  Current event: both OFF→ON, and
Circuit A, Subpanel and Main each read +250 W (± 2 W).

Asserted for all three variants (A: no models, B: 125 W models, C: steady state
one cycle later with no transition):

* exactly one ``DeviceGroupEstimate`` {L1, L2}, point 250 ± noise,
  ``GROUP_IDENTIFIABLE``, source readers ``[Circuit A, Subpanel, Main]`` and
  evidence refs naming the group and its readers;
* each member has ``point_w is None``, ``NON_IDENTIFIABLE`` and bounds
  ``[0, 250] ± noise`` with ``basis="non_negativity"``; no 125 W anywhere; the
  status is never ESTIMATED or ALLOCATED;
* ΔU ≈ 0, the residual does not contain the 250 W, and the total attributed is
  250 W — not 750 W from counting the three nested readers three times.

The adapter is the test's stand-in for I7's ``infer_group`` / ``infer_steady``
(which do not exist in this wave): I3 owns the real joint solve, so the small
deterministic least-squares below only produces the ``JointSolution`` that I3
will hand to I4.  Identifiability itself never reads that solution: it is
computed from H plus history (design §7), which is exactly what the two-pass
adapter demonstrates.
"""

from __future__ import annotations

import dataclasses
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from scipy.optimize import lsq_linear
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceModelParameters,
    DeviceModelVersion,
    DeviceState,
    DeviceTopology,
    IdentifiabilityStatus,
    ModelStatus,
    ModelType,
    Phase,
    Source,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation import DisaggregationEngine
from wattmosaic_engine.electrical import ObservationWindow
from wattmosaic_engine.inference import identifiability as identifiability_module
from wattmosaic_engine.inference.identifiability import (
    IdentifiabilityAnalyzer,
    assemble_estimates,
    subject_group_id,
)
from wattmosaic_engine.runtime.fakes import (
    FakeMapping,
    FakeTopology,
    InMemoryHistory,
    fake_operator,
)
from wattmosaic_engine.runtime.interfaces import HistoryRow, JointSolution

T0 = datetime(2026, 11, 5, 18, 0, 0, tzinfo=UTC)
CADENCE_S = 15.0
#: The readers of the reference house, most specific first (AT-07 order).
READERS = ("Circuit A", "Subpanel", "Main")
SUBJECTS = (("device", "L1"), ("device", "L2"))
GROUP_MEMBERS = ("L1", "L2")


# --------------------------------------------------------------------------- #
# Scenario
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Published:
    """One published interpretation, normalised across the two code paths."""

    points: Mapping[str, float | None]
    statuses: Mapping[str, str]
    identifiability: Mapping[str, str]
    bounds: Mapping[str, tuple[float | None, float | None]]
    basis: Mapping[str, str | None]
    group_ids: Mapping[str, str | None]
    groups: tuple[GroupView, ...]
    residual_by_reader: Mapping[str, float | None]
    unknown_total_w: float | None

    @property
    def attributed_total_w(self) -> float | None:
        """Sum of every identified member plus each group sum, counted once."""
        total: float | None = None
        for value in self.points.values():
            if value is not None:
                total = value if total is None else total + value
        for group in self.groups:
            if group.point_w is not None:
                total = group.point_w if total is None else total + group.point_w
        return total


@dataclass(frozen=True)
class GroupView:
    group_id: str
    member_ids: tuple[str, ...]
    point_w: float | None
    identifiability: str
    source_reader_ids: tuple[str, ...]
    evidence_kinds_and_ids: tuple[tuple[str, str], ...]


def _history(co_toggle_events: int = 12, solo_l1_events: int = 0) -> list[HistoryRow]:
    """12 co-toggled groups, optionally plus solo L1 groups at 100 W."""
    rows: list[HistoryRow] = []
    for index in range(co_toggle_events):
        rows.append(
            HistoryRow(
                group_id=f"hist-cotoggle-{index:02d}",
                at=T0 - timedelta(hours=6 * (index + 1)),
                subject_ids=("L1", "L2"),
                reader_ids=("Circuit A",),
                delta_w_by_reader={"Circuit A": 250.0},
            )
        )
    for index in range(solo_l1_events):
        rows.append(
            HistoryRow(
                group_id=f"hist-solo-l1-{index:02d}",
                at=T0 - timedelta(days=2, hours=index),
                subject_ids=("L1",),
                reader_ids=("Circuit A",),
                delta_w_by_reader={"Circuit A": 100.0},
            )
        )
    return rows


def _operator(*, stale_readers: Sequence[str] = (), at: datetime = T0):
    topology = FakeTopology.from_tree({"Main": {"Subpanel": {"Circuit A": {}}}})
    mapping = FakeMapping.from_pairs(
        {("device", "L1"): "Circuit A", ("device", "L2"): "Circuit A"}
    )
    operator = fake_operator(topology, mapping, READERS, SUBJECTS, at=at)
    if not stale_readers:
        return operator
    rows = tuple(
        row.__class__(**{**row.__dict__, "excluded_reason": "stale"})
        if row.reader_id in stale_readers
        else row
        for row in operator.rows
    )
    return type(operator)(
        H=operator.H,
        rows=rows,
        subjects=operator.subjects,
        entries=operator.entries,
        unknown_scopes=operator.unknown_scopes,
        soft_alternatives=operator.soft_alternatives,
        topology_version_id=operator.topology_version_id,
        mapping_version_id=operator.mapping_version_id,
        at=operator.at,
    )


def _solo_amplitudes(history: Sequence[HistoryRow]) -> dict[str, float]:
    """Median reader delta of events in which exactly one subject transitioned."""
    samples: dict[str, list[float]] = {}
    for row in history:
        if len(set(row.subject_ids)) != 1:
            continue
        subject_id = row.subject_ids[0]
        for value in row.delta_w_by_reader.values():
            if value is not None:
                samples.setdefault(subject_id, []).append(float(value))
    return {subject: statistics.median(values) for subject, values in samples.items()}


def _solve(
    operator,
    readings: Mapping[str, float],
    identified: Sequence[str],
    solo: Mapping[str, float],
) -> JointSolution:
    """A minimal deterministic stand-in for I3's joint solve.

    The current-absolute rows are ``H x = y`` for every reader with a reading.
    A subject the analysis called IDENTIFIABLE and for which history has a solo
    amplitude is pinned by one heavily weighted unit row at that amplitude; the
    pin is *evidence*, not a model prior.  Non-negativity is the only other
    constraint.  I3 replaces this with the robust signed solver; I4 only needs a
    ``JointSolution`` to read the solved sum and the posterior from.
    """
    columns = {
        subject_id: index
        for index, (_kind, subject_id) in enumerate(operator.subjects)
    }
    matrix = np.asarray(operator.H, dtype=float)
    rows: list[np.ndarray] = []
    values: list[float] = []
    for index, row in enumerate(operator.rows):
        if row.reader_id not in readings or row.excluded_reason is not None:
            continue
        rows.append(matrix[index])
        values.append(float(readings[row.reader_id]))
    weight = max(1.0, 100.0 * max(1, len(rows)))
    for subject_id in identified:
        amplitude = solo.get(subject_id)
        if amplitude is None:
            continue
        unit = np.zeros(matrix.shape[1])
        unit[columns[subject_id]] = weight
        rows.append(unit)
        values.append(float(amplitude) * weight)
    if not rows:
        return JointSolution(
            x=np.zeros(matrix.shape[1]),
            cov=None,
            u=np.zeros(0),
            active_rows=(),
            row_residual_w=np.zeros(0),
            objective=0.0,
            converged=False,
            mode="delta",
        )
    design = np.vstack(rows)
    solution = lsq_linear(
        design, np.asarray(values, dtype=float), bounds=(0.0, np.inf), method="trf"
    )
    return JointSolution(
        x=np.asarray(solution.x, dtype=float),
        cov=None,
        u=np.zeros(0),
        active_rows=tuple(range(len(readings))),
        row_residual_w=np.asarray(solution.fun, dtype=float),
        objective=float(solution.cost),
        converged=bool(solution.success),
        mode="delta",
    )


def run_new_pipeline(
    *,
    readings: Mapping[str, float],
    history: Sequence[HistoryRow],
    models: Mapping[str, float] | None = None,
    stale_readers: Sequence[str] = (),
    at: datetime = T0,
) -> Published:
    """Run the I4 pipeline: analyse → solve → analyse → assemble.

    Two analysis passes are deliberate.  The first uses a zero solution to read
    the identifiability *classification*, which by design depends only on H plus
    history (design §7) — never on this cycle's transition or on the solver.  The
    solve then uses that classification, and the second pass supplies the group
    sum and posterior.  A pipeline that decided identifiability from the solution
    would be the defect this task closes.
    """
    operator = _operator(stale_readers=stale_readers, at=at)
    history_store = InMemoryHistory(history)
    analyzer = IdentifiabilityAnalyzer()
    zero = JointSolution(
        x=np.zeros(len(operator.subjects)),
        cov=None,
        u=np.zeros(0),
        active_rows=tuple(
            index
            for index, row in enumerate(operator.rows)
            if row.excluded_reason is None
        ),
        row_residual_w=np.zeros(len(operator.rows)),
        objective=0.0,
        converged=True,
        mode="delta",
    )
    first = analyzer.analyze(operator, zero, list(GROUP_MEMBERS), history_store)
    solution = _solve(
        operator, readings, first.identifiable_ids(), _solo_amplitudes(history)
    )
    report = analyzer.analyze(operator, solution, list(GROUP_MEMBERS), history_store)
    assembly = assemble_estimates(
        report,
        operator,
        versions=_versions(),
        at=at,
        points={
            subject_id: float(solution.x[index])
            for index, (_kind, subject_id) in enumerate(operator.subjects)
        },
        models=models,
    )

    residual: dict[str, float | None] = {}
    attributed_by_reader: dict[str, float] = {reader: 0.0 for reader in readings}
    matrix = np.asarray(operator.H, dtype=float)
    for estimate in assembly.device_estimates:
        if estimate.point_w is None:
            continue
        column = operator.subjects.index((estimate.subject_kind, estimate.subject_id))
        for index, row in enumerate(operator.rows):
            if row.reader_id in attributed_by_reader:
                attributed_by_reader[row.reader_id] += (
                    matrix[index, column] * estimate.point_w
                )
    for group in assembly.group_estimates:
        for member in group.member_ids:
            column = operator.subjects.index(("device", member))
            for index, row in enumerate(operator.rows):
                if row.reader_id in attributed_by_reader and group.point_w is not None:
                    attributed_by_reader[row.reader_id] += (
                        matrix[index, column] * group.point_w / len(group.member_ids)
                    )
    for reader, value in readings.items():
        residual[reader] = float(value) - attributed_by_reader[reader]

    return Published(
        points={estimate.subject_id: estimate.point_w for estimate in assembly.device_estimates},
        statuses={
            estimate.subject_id: str(estimate.status)
            for estimate in assembly.device_estimates
        },
        identifiability={
            estimate.subject_id: str(estimate.identifiability)
            for estimate in assembly.device_estimates
        },
        bounds={
            estimate.subject_id: (estimate.bounds.lower_w, estimate.bounds.upper_w)
            for estimate in assembly.device_estimates
        },
        basis={
            estimate.subject_id: estimate.bounds.basis
            for estimate in assembly.device_estimates
        },
        group_ids={
            estimate.subject_id: estimate.group_id
            for estimate in assembly.device_estimates
        },
        groups=tuple(
            GroupView(
                group_id=group.group_id,
                member_ids=tuple(group.member_ids),
                point_w=group.point_w,
                identifiability=str(group.identifiability),
                source_reader_ids=tuple(group.source_reader_ids),
                evidence_kinds_and_ids=tuple(
                    (str(ref.kind), ref.id) for ref in group.evidence_refs
                ),
            )
            for group in assembly.group_estimates
        ),
        residual_by_reader=residual,
        unknown_total_w=sum(value for value in residual.values() if value is not None),
    )


# --------------------------------------------------------------------------- #
# Legacy path (unchanged engine, so the test proves the old defect).
# --------------------------------------------------------------------------- #
def _channel(cid: str, ct_type: CTType = CTType.BRANCH, *, parent: str | None = None):
    return CTChannel(
        ct_id=cid,
        entity_id=f"sensor.{cid}",
        ct_type=ct_type,
        phase=Phase.L1,
        parent_ct_id=parent,
        topology_status=TopologyStatus.CONFIRMED,
    )


def _legacy_topology() -> Topology:
    return Topology(
        version=1,
        status=TopologyStatus.CONFIRMED,
        channels=[
            _channel("Main", CTType.MAIN),
            _channel("Subpanel", parent="Main"),
            _channel("Circuit A", parent="Subpanel"),
        ],
        devices=[
            DeviceTopology(
                device_id=member,
                branch_ct_ids=["Circuit A"],
                phase=Phase.L1,
                topology_status=TopologyStatus.CONFIRMED,
            )
            for member in GROUP_MEMBERS
        ],
    )


def _legacy_model(device_id: str, on_w: float) -> DeviceModelVersion:
    return DeviceModelVersion(
        device_id=device_id,
        model_type=ModelType.BINARY,
        version=1,
        status=ModelStatus.ACTIVE,
        sample_count=120,
        training_quality=0.9,
        validation_metrics={"r2": 0.9},
        confidence=0.85,
        parameters=DeviceModelParameters(on_power_w=on_w, off_power_w=0.0),
    )


def _legacy_window(values: Mapping[str, float], *, transition: bool) -> ObservationWindow:
    history = {
        reader: [
            (T0 - timedelta(seconds=CADENCE_S * 5) + timedelta(seconds=CADENCE_S * i), value)
            for i in range(5)
        ]
        for reader, value in values.items()
    }
    transitions = (
        [
            dict(device_id="L1", previous_state="off", new_state="on"),
            dict(device_id="L2", previous_state="off", new_state="on"),
        ]
        if transition
        else None
    )
    return ObservationWindow.from_values(
        T0,
        dict(values),
        histories=history,
        cadence_s=CADENCE_S,
        previous_states=(
            {item["device_id"]: item["previous_state"] for item in transitions}
            if transitions
            else None
        ),
        state_transitions=transitions,
    )


def _legacy_status(result) -> str:
    if result.source == Source.UNKNOWN:
        return "unknown"
    return "estimated"


def run_legacy(*, values: Mapping[str, float], models: Mapping[str, float], transition: bool) -> Published:
    """Run the unmodified ``DisaggregationEngine.infer`` and normalise it."""
    batch = DisaggregationEngine().infer(
        _legacy_window(values, transition=transition),
        device_states={
            member: DeviceState(entity_id=f"switch.{member}", state="on")
            for member in GROUP_MEMBERS
        },
        models={device_id: _legacy_model(device_id, on_w) for device_id, on_w in models.items()},
        topology=_legacy_topology(),
    )
    ambiguous = set(batch.diagnostics.get("ambiguity", {}).get("devices", []))
    points: dict[str, float | None] = {}
    statuses: dict[str, str] = {}
    ident: dict[str, str] = {}
    bounds: dict[str, tuple[float | None, float | None]] = {}
    basis: dict[str, str | None] = {}
    group_ids: dict[str, str | None] = {}
    for result in batch.results:
        points[result.device_id] = None if result.source == Source.UNKNOWN else result.power_w
        statuses[result.device_id] = _legacy_status(result)
        ident[result.device_id] = (
            "non_identifiable"
            if result.device_id in ambiguous
            or result.provenance.get("identifiable") is False
            else "identifiable"
        )
        bounds[result.device_id] = (result.min_w, result.max_w)
        basis[result.device_id] = "non_negativity" if result.min_w is not None else None
        group_ids[result.device_id] = None
    return Published(
        points=points,
        statuses=statuses,
        identifiability=ident,
        bounds=bounds,
        basis=basis,
        group_ids=group_ids,
        groups=(),
        residual_by_reader={"Circuit A": None, "Subpanel": None, "Main": None},
        unknown_total_w=batch.reconciliation.unknown_residual_w,
    )


def _versions():
    from wattmosaic_engine.contracts import VersionTriple

    return VersionTriple(
        topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-test"
    )


# --------------------------------------------------------------------------- #
# Assertions shared by A, B, C and the positive control.
# --------------------------------------------------------------------------- #
def _assert_group_not_fabricated(published: Published, *, point_w: float = 250.0, noise: float = 3.0) -> None:
    # Exactly one group estimate, with the identified sum and honest provenance.
    assert len(published.groups) == 1
    group = published.groups[0]
    assert set(group.member_ids) == set(GROUP_MEMBERS)
    assert group.identifiability == "group_identifiable"
    assert group.point_w is not None
    assert group.point_w == pytest.approx(point_w, abs=noise)
    assert group.source_reader_ids == READERS
    refs = set(group.evidence_kinds_and_ids)
    assert any(kind == "event_group" for kind, _id in refs), refs
    assert ("event_group", "hist-cotoggle-00") in refs or point_w != 250.0
    for reader in READERS:
        assert ("observation", reader) in refs

    # Members: never a number, never ESTIMATED/ALLOCATED.
    for member in GROUP_MEMBERS:
        assert published.points[member] is None
        assert published.identifiability[member] == "non_identifiable"
        assert published.statuses[member] == "unknown"
        lower, upper = published.bounds[member]
        assert lower == pytest.approx(0.0, abs=noise)
        assert upper == pytest.approx(point_w, abs=noise)
        assert lower is not None and upper is not None and lower < upper
        assert published.basis[member] == "non_negativity"
        assert published.group_ids[member] == group.group_id

    # No fabricated split anywhere: 125 W is the equal split of 250 W and must
    # never appear as a member value or a bound.
    for value in published.points.values():
        assert value != pytest.approx(125.0, abs=1e-9)

    # ΔU ≈ 0 and the 250 W is attributed once, not once per nested reader.
    assert published.unknown_total_w == pytest.approx(0.0, abs=noise)
    for reader, residual in published.residual_by_reader.items():
        assert residual == pytest.approx(0.0, abs=noise), reader
    assert published.attributed_total_w == pytest.approx(point_w, abs=noise)


# --------------------------------------------------------------------------- #
# Variants A, B, C — identical member output.
# --------------------------------------------------------------------------- #
def test_variant_a_transition_no_models():
    published = run_new_pipeline(
        readings={"Circuit A": 250.0, "Subpanel": 249.7, "Main": 250.4},
        history=_history(),
    )
    _assert_group_not_fabricated(published)


def test_variant_b_models_declare_125_w_each():
    published = run_new_pipeline(
        readings={"Circuit A": 250.0, "Subpanel": 249.7, "Main": 250.4},
        history=_history(),
        models={"L1": 125.0, "L2": 125.0},
    )
    _assert_group_not_fabricated(published)
    # A model prior is not evidence: it changes no published member value.
    assert 125.0 not in published.points.values()


def test_variant_c_steady_state_one_cycle_later():
    published = run_new_pipeline(
        readings={"Circuit A": 250.0, "Subpanel": 249.7, "Main": 250.4},
        history=_history(),
        models={"L1": 125.0, "L2": 125.0},
        at=T0 + timedelta(seconds=CADENCE_S),
    )
    _assert_group_not_fabricated(published)


def test_the_three_variants_agree_on_members():
    """A, B and C must publish byte-identical member facts."""
    readings = {"Circuit A": 250.0, "Subpanel": 249.7, "Main": 250.4}
    first = run_new_pipeline(readings=readings, history=_history())
    second = run_new_pipeline(
        readings=readings, history=_history(), models={"L1": 125.0, "L2": 125.0}
    )
    third = run_new_pipeline(
        readings=readings,
        history=_history(),
        models={"L1": 125.0, "L2": 125.0},
        at=T0 + timedelta(seconds=CADENCE_S),
    )
    for other in (second, third):
        for member in GROUP_MEMBERS:
            assert other.points[member] == first.points[member]
            assert other.identifiability[member] == first.identifiability[member]
            assert other.bounds[member] == first.bounds[member]
            assert other.statuses[member] == first.statuses[member]
        assert other.groups[0].group_id == first.groups[0].group_id


# --------------------------------------------------------------------------- #
# The legacy engine must FAIL the same assertions (the test proves DEF-04).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("label", "models", "values", "transition"),
    [
        (
            "A transition, no models",
            {},
            {"Main": 250.0, "Subpanel": 249.7, "Circuit A": 250.4},
            True,
        ),
        (
            "B transition, 125 W models",
            {"L1": 125.0, "L2": 125.0},
            {"Main": 250.0, "Subpanel": 249.7, "Circuit A": 250.4},
            True,
        ),
        (
            "C steady state, 125 W models",
            {"L1": 125.0, "L2": 125.0},
            {"Main": 250.0, "Subpanel": 249.7, "Circuit A": 250.4},
            False,
        ),
    ],
)
def test_the_legacy_engine_cannot_satisfy_the_group_contract(label, models, values, transition):
    legacy = run_legacy(values=values, models=models, transition=transition)
    with pytest.raises(AssertionError):
        _assert_group_not_fabricated(legacy)


def test_the_legacy_probe_range_excludes_the_measured_sum():
    """The old ``max_w=337.5 < 500`` defect: a published range must contain the sum.

    Two 250 W models on one 500 W row published ``[250, 337.5]`` for each member
    — a range that does not contain the 500 W actually measured.  No interval in
    the new output may exclude the measured value it describes.
    """
    legacy = run_legacy(
        values={"Main": 500.0, "Subpanel": 500.0, "Circuit A": 500.0},
        models={"L1": 250.0, "L2": 250.0},
        transition=True,
    )
    measured = 500.0

    def assert_range_contains_the_measured_sum(published: Published) -> None:
        for member in GROUP_MEMBERS:
            lower, upper = published.bounds[member]
            assert lower is not None and upper is not None
            # This exact assertion fails on the legacy engine: the old code
            # published [250, 337.5] for a 500 W row.
            assert lower <= measured <= upper, (
                f"range for {member} is [{lower}, {upper}], which excludes the "
                f"measured {measured} W"
            )

    with pytest.raises(AssertionError):
        assert_range_contains_the_measured_sum(legacy)

    fixed = run_new_pipeline(
        readings={"Circuit A": 500.0, "Subpanel": 500.0, "Main": 500.0},
        history=[
            HistoryRow(
                group_id=f"hist-500-{index}",
                at=T0 - timedelta(hours=index + 1),
                subject_ids=("L1", "L2"),
                reader_ids=("Circuit A",),
                delta_w_by_reader={"Circuit A": 500.0},
            )
            for index in range(12)
        ],
        models={"L1": 250.0, "L2": 250.0},
    )
    _assert_group_not_fabricated(fixed, point_w=500.0)
    # Each member's own interval also contains the measured sum: it is a bound
    # on what the member *could* be, and no member can exceed the group.
    for member in GROUP_MEMBERS:
        lower, upper = fixed.bounds[member]
        assert lower is not None and upper is not None
        assert lower <= measured <= upper


# --------------------------------------------------------------------------- #
# Positive control: solo history separates the pair.
# --------------------------------------------------------------------------- #
def test_positive_control_solo_history_identifies_both_lights():
    published = run_new_pipeline(
        readings={"Circuit A": 250.0, "Subpanel": 249.7, "Main": 250.4},
        history=_history(solo_l1_events=3),
        models={"L1": 125.0, "L2": 125.0},
    )
    assert published.groups == ()
    for member in GROUP_MEMBERS:
        assert published.identifiability[member] == "identifiable"
        assert published.statuses[member] == "estimated"
        assert published.group_ids[member] is None
        lower, upper = published.bounds[member]
        assert lower is not None and upper is not None
        assert lower <= published.points[member] <= upper
    assert published.points["L1"] == pytest.approx(100.0, abs=5.0)
    assert published.points["L2"] == pytest.approx(150.0, abs=5.0)


# --------------------------------------------------------------------------- #
# Mutation guards: each mutated pipeline must make the assertion above fail.
# --------------------------------------------------------------------------- #
def _force_identifiable(monkeypatch) -> None:
    """Mutation: decide identifiability from this cycle's transition, as the
    legacy engine did, instead of from H plus history (trap 3 / C9).

    Every candidate becomes IDENTIFIABLE and no group is published, which is
    what let the old per-device path fabricate 125 W each.
    """

    real = IdentifiabilityAnalyzer.analyze

    def transition_based(self, operator, solution, candidates, history):
        report = real(self, operator, solution, candidates, history)
        return dataclasses.replace(
            report,
            subjects={subject_id: IdentifiabilityStatus.IDENTIFIABLE for subject_id in report.subject_ids},
            groups=(),
        )

    monkeypatch.setattr(IdentifiabilityAnalyzer, "analyze", transition_based)


def _assert_mutation_is_caught(mutated: Published) -> None:
    with pytest.raises(AssertionError):
        _assert_group_not_fabricated(mutated)


def test_mutation_solver_equal_split_is_rejected(monkeypatch):
    """The solver patched to return the equal split must fail the assertions.

    With identifiability forced to "this cycle transitions" (the old defect) the
    pipeline happily publishes the split, so the guard has to catch the 125 W
    member values themselves.
    """

    real_solve = _solve

    def equal_split(*args, **kwargs):
        solution = real_solve(*args, **kwargs)
        return JointSolution(
            x=np.full_like(solution.x, 125.0),
            cov=None,
            u=solution.u,
            active_rows=solution.active_rows,
            row_residual_w=solution.row_residual_w,
            objective=solution.objective,
            converged=solution.converged,
            mode=solution.mode,
        )

    _force_identifiable(monkeypatch)
    monkeypatch.setattr("tests.unit.test_group_250w._solve", equal_split)
    mutated = run_new_pipeline(
        readings={"Circuit A": 250.0, "Subpanel": 249.7, "Main": 250.4},
        history=_history(),
    )
    assert mutated.points["L1"] == pytest.approx(125.0)  # the mutation took effect
    _assert_mutation_is_caught(mutated)


def test_mutation_member_filled_from_model_is_rejected(monkeypatch):
    """Estimate assembly patched to fill members from their 125 W models."""

    _force_identifiable(monkeypatch)
    monkeypatch.setattr(
        identifiability_module,
        "member_point_w",
        lambda *, solution_w, model_w, identifiability: model_w,
    )
    mutated = run_new_pipeline(
        readings={"Circuit A": 250.0, "Subpanel": 249.7, "Main": 250.4},
        history=_history(),
        models={"L1": 125.0, "L2": 125.0},
    )
    assert mutated.points["L1"] == pytest.approx(125.0)  # the mutation took effect
    _assert_mutation_is_caught(mutated)


def test_mutation_zero_width_bounds_are_rejected(monkeypatch):
    """Bounds collapsed to ``[250, 250]`` must fail the assertions."""
    from wattmosaic_engine.contracts import Bounds
    from wattmosaic_engine.inference import bounds as bounds_module

    real = bounds_module.compute_bounds

    def collapsed(subject_ids, **kwargs):
        result = real(subject_ids, **kwargs)
        return {subject_id: Bounds._point(250.0, "group_sum") for subject_id in result}

    monkeypatch.setattr(
        "wattmosaic_engine.inference.identifiability.compute_bounds", collapsed
    )
    mutated = run_new_pipeline(
        readings={"Circuit A": 250.0, "Subpanel": 249.7, "Main": 250.4},
        history=_history(),
    )
    assert mutated.bounds["L1"] == (250.0, 250.0)  # the mutation took effect
    _assert_mutation_is_caught(mutated)


def test_group_id_is_the_stable_member_hash():
    published = run_new_pipeline(
        readings={"Circuit A": 250.0, "Subpanel": 250.0, "Main": 250.0},
        history=_history(),
    )
    assert published.groups[0].group_id == subject_group_id(GROUP_MEMBERS)
    assert published.group_ids["L1"] == published.group_ids["L2"]


def test_a_group_id_is_stable_over_100_cycles():
    """The id depends on membership only, never on this cycle's numbers."""
    expected = subject_group_id(GROUP_MEMBERS)
    for cycle in range(100):
        noise = float(np.sin(cycle)) * 2.0
        published = run_new_pipeline(
            readings={
                "Circuit A": 250.0 + noise,
                "Subpanel": 250.0 - noise,
                "Main": 250.0,
            },
            history=_history(),
            at=T0 + timedelta(seconds=CADENCE_S * cycle),
        )
        assert published.groups[0].group_id == expected
        assert published.group_ids["L1"] == expected
