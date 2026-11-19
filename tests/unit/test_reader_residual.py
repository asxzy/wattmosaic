"""T3: per-reader residual engine and learned unknown-load prior (design §6).

Coverage maps to the task's test list:

* two scopes with different loads get different residuals/baselines (DEF-05);
* the signed identity holds and is never clamped (negative stays negative);
* a missing Y_r gives a ``None`` residual (never 0 W);
* λ_u decreases as the unexplained step rate rises, and the initial λ_u
  favours residual on an ambiguous synthetic case;
* noise, residual and profile are three distinct objects with distinct storage;
* identified subjects and group sums count once;
* the background job recomputes a scope's prior from a 7-day window with an
  injected clock, and the model is a ``ResidualModelView``;
* the per-reader per-day over/under-assignment diagnostic;
* persistence goes through S19's batch ``put_points`` in one call.

Production code never imports ``runtime.fakes``; the tests use those fakes.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from wattmosaic_engine.contracts import (
    Bounds,
    CandidateHypothesis,
    ConfidenceVectorV3,
    DeviceEstimate,
    DeviceGroupEstimate,
    EstimateStatus,
    EventGroupStatus,
    IdentifiabilityStatus,
    InferenceMethod,
    JointInferenceResult,
    MeasurementBasis,
    UnknownLoadProfileV3,
    VersionTriple,
)
from wattmosaic_engine.inference.joint import solve_joint_detailed
from wattmosaic_engine.residual import (
    DEFAULT_PENALTY_SCALE,
    INITIAL_PENALTY_SCALE_FRACTION,
    PRIOR_WINDOW_DAYS,
    ReaderResidualEngine,
    ResidualPriorJob,
    UnknownPriorModel,
    assignment_diagnostics,
    estimate_unknown_prior,
)
from wattmosaic_engine.residual.reader_residual import (
    AssignmentDiagnostics,
    UnexplainedStep,
    baseline_and_dynamic,
    signed_residual_w,
)
from wattmosaic_engine.runtime.interfaces import (
    HeldReading,
    ObservationOperator,
    ResidualModelView,
    RowMeta,
)

from tests.support.fakes import (
    HB17_TREE,
    FakeMapping,
    FakeTopology,
    fake_operator,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
READERS = ("Main", "Subpanel", "Circuit A", "Circuit B", "Circuit C")
SUBJECTS = (("device", "lights"), ("device", "kettle"))
VERSIONS = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)


# --------------------------------------------------------------------------- #
# Helpers — only K08 fakes, no real clock/DB in production code.
# --------------------------------------------------------------------------- #


def _held(value: float | None) -> HeldReading:
    return HeldReading(
        value=value,
        held_since=T0,
        last_confirmed_at=T0,
        state="held" if value is not None else "missing",
    )


def _estimate(
    subject_id: str, point: float | None, *, group_id: str | None = None
) -> DeviceEstimate:
    return DeviceEstimate(
        subject_kind="device",
        subject_id=subject_id,
        point_w=point,
        bounds=Bounds(lower_w=None, upper_w=None, basis=None),
        status=(
            EstimateStatus.ESTIMATED if point is not None else EstimateStatus.UNKNOWN
        ),
        identifiability=(
            IdentifiabilityStatus.IDENTIFIABLE
            if point is not None
            else IdentifiabilityStatus.NON_IDENTIFIABLE
        ),
        group_id=group_id,
        method=InferenceMethod.JOINT_DELTA,
        confidence=ConfidenceVectorV3(),
        source_reader_ids=["Circuit A"],
        evidence_refs=[],
        versions=VERSIONS,
        updated_at=T0,
        provisional=False,
    )


def _group(group_id: str, members: list[str], point: float | None) -> DeviceGroupEstimate:
    return DeviceGroupEstimate(
        group_id=group_id,
        member_ids=members,
        point_w=point,
        bounds=Bounds(lower_w=None, upper_w=None, basis=None),
        identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
        source_reader_ids=["Main"],
        evidence_refs=[],
        versions=VERSIONS,
        updated_at=T0,
    )


def _result(*estimates: DeviceEstimate, groups: tuple[DeviceGroupEstimate, ...] = ()) -> JointInferenceResult:
    return JointInferenceResult(
        event_group_id="g1",
        event_group_revision=1,
        status=EventGroupStatus.CONFIRMED,
        device_estimates=list(estimates),
        group_estimates=list(groups),
        unknown_estimates=[],
        candidates=[],
        matches={},
        residual_w_by_reader={},
        versions=VERSIONS,
        computed_at=T0,
    )


def _operator(subjects: tuple[tuple[str, str], ...] = SUBJECTS) -> ObservationOperator:
    topo = FakeTopology.from_tree(HB17_TREE, at=T0)
    mapping = FakeMapping.from_pairs(
        {("device", "lights"): "Circuit A", ("device", "kettle"): "Circuit C"}
    )
    return fake_operator(topo, mapping, READERS, subjects, at=T0)


def _point(reader: str, value: float | None, at: datetime = T0, *, dynamic: float = 0.0):
    from wattmosaic_engine.contracts import ReaderResidualPoint

    return ReaderResidualPoint(
        reader_id=reader,
        at=at,
        residual_w=value,
        baseline_w=value,
        dynamic_w=dynamic,
        noise_sigma_w=None,
        topology_version_id="topo-1",
        basis=MeasurementBasis.CT_MEASURED,
    )


# --------------------------------------------------------------------------- #
# Signed residual and the DEF-05 per-scope regression.
# --------------------------------------------------------------------------- #


def test_signed_identity_is_never_clamped() -> None:
    assert signed_residual_w(100.0, 250.0) == pytest.approx(-150.0)
    assert signed_residual_w(0.0, 0.0) == pytest.approx(0.0)
    # The engine preserves the sign; no max(0, ...) anywhere.
    op = _operator()
    engine = ReaderResidualEngine()
    result = _result(_estimate("lights", 300.0), _estimate("kettle", 400.0))
    held = {
        "Main": _held(100.0),
        "Subpanel": _held(400.0),
        "Circuit A": _held(300.0),
        "Circuit B": _held(50.0),
        "Circuit C": _held(400.0),
    }
    points = {p.reader_id: p for p in engine.update(result, held, op)}
    assert points["Main"].residual_w is not None
    assert points["Main"].residual_w < 0.0
    assert points["Subpanel"].residual_w is not None
    # For a leaf reader with a subject, R = Y - P̂ exactly.
    assert points["Circuit A"].residual_w == pytest.approx(300.0 - 300.0)
    assert points["Circuit C"].residual_w == pytest.approx(400.0 - 400.0)


def test_baseline_and_dynamic_split_and_missing_point_is_none() -> None:
    # The robust baseline/dynamic split; absent residuals stay None (never a
    # fabricated 0 W).
    assert baseline_and_dynamic(None, (1.0, 2.0)) == (None, None)
    baseline, dynamic = baseline_and_dynamic(50.0, (10.0, 20.0, 30.0))
    assert baseline == pytest.approx(20.0)
    assert dynamic == pytest.approx(30.0)
    baseline, dynamic = baseline_and_dynamic(-5.0, ())
    assert baseline == pytest.approx(-5.0)
    assert dynamic == pytest.approx(0.0)

    op = _operator()
    engine = ReaderResidualEngine()
    result = _result()
    held = {r: _held(None) for r in READERS}
    points = {p.reader_id: p for p in engine.update(result, held, op)}
    assert all(p.residual_w is None for p in points.values())
    assert all(p.baseline_w is None for p in points.values())


def test_two_scopes_with_different_loads_get_different_baselines() -> None:
    """DEF-05: a per-reader window cannot be replaced by one install-wide one."""
    op = _operator()
    engine = ReaderResidualEngine()
    result = _result(_estimate("lights", 100.0), _estimate("kettle", 200.0))
    held = {
        "Main": _held(400.0),
        "Subpanel": _held(150.0),
        "Circuit A": _held(150.0),
        "Circuit B": _held(50.0),
        "Circuit C": _held(250.0),
    }
    windows = {"Main": (100.0, 110.0, 120.0), "Circuit A": (40.0, 50.0, 60.0)}
    points = {p.reader_id: p for p in engine.update(result, held, op, windows=windows)}
    assert points["Main"].baseline_w == pytest.approx(110.0)
    assert points["Circuit A"].baseline_w == pytest.approx(50.0)
    assert points["Main"].baseline_w != pytest.approx(points["Circuit A"].baseline_w)
    # Dynamic adds back to the residual for each scope separately.
    for reader in ("Main", "Circuit A"):
        p = points[reader]
        assert p.baseline_w is not None and p.dynamic_w is not None and p.residual_w is not None
        assert p.baseline_w + p.dynamic_w == pytest.approx(p.residual_w, abs=1e-9)


def test_missing_y_r_gives_a_none_residual_not_zero() -> None:
    op = _operator()
    engine = ReaderResidualEngine()
    result = _result(_estimate("lights", 100.0))
    held = {"Circuit A": _held(None)}
    points = {p.reader_id: p for p in engine.update(result, held, op)}
    assert points["Circuit A"].residual_w is None
    assert points["Circuit A"].residual_w != 0.0


def test_identified_subjects_and_group_sums_count_once() -> None:
    """A group member never appears both as itself and via the group sum."""
    op = _operator((("device", "a"), ("device", "b")))
    topo = FakeTopology.from_tree(HB17_TREE, at=T0)
    mapping = FakeMapping.from_pairs(
        {("device", "a"): "Circuit A", ("device", "b"): "Circuit A"}
    )
    op = fake_operator(topo, mapping, READERS, (("device", "a"), ("device", "b")), at=T0)
    engine = ReaderResidualEngine()
    result = _result(
        _estimate("a", 10.0, group_id="g"),
        _estimate("b", 20.0, group_id="g"),
        groups=(_group("g", ["a", "b"], 60.0),),
    )
    held = {
        "Circuit A": _held(100.0),
        "Subpanel": _held(100.0),
        "Main": _held(100.0),
    }
    points = {p.reader_id: p for p in engine.update(result, held, op)}
    # On Circuit A the group sum (60) counts once, not 10+20+60 = 90.
    assert points["Circuit A"].residual_w == pytest.approx(100.0 - 60.0)
    # On the parent the child measurement (100) accounts for the whole scope;
    # its subject enters as EXPECTED and is not counted a second time.
    assert points["Subpanel"].residual_w == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Noise vs residual vs profile — three distinct objects, distinct storage.
# --------------------------------------------------------------------------- #


def test_noise_residual_and_profile_are_three_distinct_objects() -> None:
    op = _operator()
    sigma_lookup: Mapping[str, float | None] = {"Circuit A": 1.5}
    engine = ReaderResidualEngine(noise_sigma_w=sigma_lookup)
    result = _result(_estimate("lights", None))
    held = {"Circuit A": _held(123.0)}
    point = engine.update(result, held, op)[2]
    assert point.residual_w == pytest.approx(123.0)
    assert point.noise_sigma_w == pytest.approx(1.5)

    # Noise lives on the point's σ field, residual on residual_w, and the
    # profile is a separate contract type for T6 (never stored in either).
    profile = UnknownLoadProfileV3(
        profile_id="p1",
        scope_endpoint_id="Circuit A",
        lifecycle="observing",
        signature={"delta_w_median": 123.0},
        first_seen=T0,
        last_seen=T0,
    )
    assert profile.scope_endpoint_id != point.reader_id or profile.profile_id
    assert point.noise_sigma_w != point.residual_w
    assert not hasattr(point, "profile_id")


def test_persistence_goes_through_s19_batch_sink() -> None:
    class RecordingSink:
        def __init__(self) -> None:
            self.batches: list[list[object]] = []

        def put_points(self, batch):
            self.batches.append(list(batch))
            return len(batch)

    op = _operator()
    sink = RecordingSink()
    engine = ReaderResidualEngine()
    result = _result()
    held = {r: _held(1.0) for r in READERS}
    points = engine.update(result, held, op, sink=sink)
    assert len(sink.batches) == 1
    assert len(sink.batches[0]) == len(points) == len(READERS)
    assert all(p.topology_version_id == "topo-1" for p in points)


# --------------------------------------------------------------------------- #
# Learned unknown-load prior.
# --------------------------------------------------------------------------- #


def _steps(scope: str, deltas: list[float], *, at: datetime = T0) -> list[UnexplainedStep]:
    return [UnexplainedStep(at=at - timedelta(hours=1), delta_w=d) for d in deltas]


def test_lambda_u_decreases_as_unexplained_step_rate_rises() -> None:
    quiet = estimate_unknown_prior("Main", _steps("Main", [100.0]), at=T0)
    busy = estimate_unknown_prior("Main", _steps("Main", [100.0] * 50), at=T0)
    assert busy.step_rate_per_h is not None and quiet.step_rate_per_h is not None
    assert busy.step_rate_per_h > quiet.step_rate_per_h
    assert busy.lambda_u < quiet.lambda_u
    assert quiet.lambda_u == pytest.approx(DEFAULT_PENALTY_SCALE / (1.0 + quiet.step_rate_per_h))


def test_prior_window_is_seven_days_and_initial_prior_favours_residual() -> None:
    assert PRIOR_WINDOW_DAYS == 7.0
    old = UnexplainedStep(at=T0 - timedelta(days=8), delta_w=200.0)
    prior = estimate_unknown_prior("Main", [old], at=T0)
    assert prior.step_rate_per_h == pytest.approx(0.0)
    assert prior.lambda_u == pytest.approx(INITIAL_PENALTY_SCALE_FRACTION * DEFAULT_PENALTY_SCALE)
    assert prior.sigma_w > 0.0  # a zero MAD is never a fabricated zero σ

    model = UnknownPriorModel()
    assert isinstance(model, ResidualModelView)
    initial = model.unknown_prior("Main", T0)
    assert initial is not None
    assert initial.lambda_u == pytest.approx(0.25 * DEFAULT_PENALTY_SCALE)
    # After a learned prior lands, the view publishes it.
    learned = estimate_unknown_prior("Main", _steps("Main", [10.0] * 20), at=T0)
    model.update(learned)
    assert model.unknown_prior("Main", T0) == learned
    # A scope with no learned prior still gets the initial, cheap unknown.
    assert model.unknown_prior("Circuit A", T0) == model.initial_prior("Circuit A")


def test_background_job_recomputes_priors_with_injected_clock() -> None:
    model = UnknownPriorModel(penalty_scale=2.0)

    def clock() -> datetime:
        return T0

    job = ResidualPriorJob(model, clock=clock)
    priors = job.run(
        {
            "Main": _steps("Main", [100.0, 120.0, 90.0, 110.0]),
            "Circuit A": _steps("Circuit A", [10.0] * 40),
        }
    )
    assert set(priors) == {"Main", "Circuit A"}
    assert priors["Circuit A"].lambda_u < priors["Main"].lambda_u
    assert model.unknown_prior("Main", T0) == priors["Main"]
    assert model.unknown_prior("Circuit A", T0) == priors["Circuit A"]
    # μ_U = median, σ_U = 1.4826·MAD.
    main = priors["Main"]
    assert main.mu_w == pytest.approx(105.0)
    assert main.sigma_w == pytest.approx(1.4826 * 10.0)


def test_initial_lambda_u_favours_residual_over_a_weak_candidate() -> None:
    """Ambiguous synthetic case: more power lands in ΔU than a weak candidate.

    With a weak candidate (a model prior centred near zero with the same scale
    as the unknown competitor), the only asymmetry is the penalty weight: I3's
    model λ_m = 1.0 versus T3's initial λ_u = 0.25 × scale.  The cheaper unknown
    competitor therefore absorbs more of the ambiguous step than the candidate.
    This is DEC-23's "preserve the residual" operating point, exercised through
    I3's joint solve.
    """
    from wattmosaic_engine.contracts import DeltaPrior

    subjects = (("device", "weak"),)
    op = ObservationOperator(
        H=np.array([[1.0]]),
        rows=(RowMeta("Main", "reader", 2.0, 1.0),),
        subjects=subjects,
        entries={},
        unknown_scopes=(("Main", (0,)),),
        soft_alternatives=(),
        topology_version_id="topo-1",
        mapping_version_id="map-1",
        at=T0,
    )
    candidate = CandidateHypothesis(
        candidate_id="c-weak",
        subject_kind="device",
        subject_id="weak",
        sources=["ha_state"],
        transition="on",
        evidence_terms={},
        filtered=False,
        filter_reason=None,
    )
    # Equal σ for both competitors, so only the penalty weight decides the
    # split: λ_m = 1.0 (I3 default) vs T3's initial λ_u = 0.25 × scale.
    known_prior = DeltaPrior(
        mean_w=0.0, sigma_w=200.0, sign="pos", model_version_id="mdl-weak"
    )
    # The initial prior is what T3 publishes before a scope is characterised.
    prior = UnknownPriorModel().unknown_prior("Main", T0)
    assert prior is not None
    assert prior.sigma_w == pytest.approx(200.0)
    report = solve_joint_detailed(
        op,
        {"Main": 100.0},
        {"Main": 2.0},
        [candidate],
        {"weak": known_prior},
        {"Main": prior},
    )
    unknown_power = abs(float(report.solution.u[0]))
    candidate_power = abs(float(report.solution.x[0]))
    assert unknown_power > candidate_power
    assert report.unknown_share_by_reader["Main"] is not None
    assert report.unknown_share_by_reader["Main"] > 0.5


def test_assignment_diagnostic_reports_per_reader_day_shares() -> None:
    points = [
        _point("Main", 100.0, dynamic=70.0),
        _point("Main", 100.0, dynamic=70.0),
        _point("Circuit A", 20.0, dynamic=0.0),
    ]
    diags = {d.scope_reader_id: d for d in assignment_diagnostics(points, day=T0)}
    assert set(diags) == {"Main", "Circuit A"}
    main: AssignmentDiagnostics = diags["Main"]
    assert main.unexplained_share is not None and main.unexplained_share > 0.0
    assert main.residual_share is not None and main.residual_share >= 0.0
    assert main.subject_share is not None and 0.0 <= main.subject_share <= 1.0
    # A missing residual never becomes a 0-share diagnostic row.
    assert all(p.residual_w is not None for p in points)
    assert assignment_diagnostics([_point("Main", None)]) == ()


# --------------------------------------------------------------------------- #
# HTTP v3 route: GET /residuals?reader_id=
# --------------------------------------------------------------------------- #


def test_residuals_v3_route_reports_points_prior_and_diagnostic() -> None:
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer
    from wattmosaic_engine.api.facade import StubFacade
    from wattmosaic_engine.api.server import create_app

    point = _point("Circuit A", 42.0, at=T0, dynamic=20.0)

    class ResidualFacade(StubFacade):
        def reader_residual_points(self, reader_id: str):
            return [point] if reader_id == "Circuit A" else []

    async def scenario() -> None:
        app = create_app(ResidualFacade())
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/residuals?reader_id=Circuit%20A")
            assert resp.status == 200
            body = await resp.json()
            assert [p["residual_w"] for p in body["points"]] == [42.0]
            assert body["points"][0]["topology_version_id"] == "topo-1"
            assert body["unknown_prior"]["scope_reader_id"] == "Circuit A"
            assert body["unknown_prior"]["lambda_u"] == pytest.approx(
                INITIAL_PENALTY_SCALE_FRACTION * DEFAULT_PENALTY_SCALE
            )
            assert body["diagnostics"][0]["scope_reader_id"] == "Circuit A"
            # Without reader_id the legacy component payload is untouched.
            resp = await client.get("/api/v1/residuals")
            assert resp.status == 200
            legacy = await resp.json()
            assert "components" in legacy

    asyncio.run(scenario())
