"""Z6 correctness regressions (HB §18: absent is None, never 0).

* an unknown scope whose observation row had no sigma is excluded from the
  solve and must be published as ``delta_w=None``, never 0.0;
* a genuinely constrained scope with a real 0 W solution still reports 0.0;
* the snapshot assembler sums unknown estimates with the same rule it uses for
  identified/group power (0.0 over empty, sum when all present, None if any is
  absent);
* ``ReconciliationV3.balance_error_w`` is computable once every term is present;
* a stored exact-solve ``[p, p]`` estimate bound is restored on read by both the
  event-record reader and the snapshot reader (shared repair helper), rather
  than raising ``ValidationError``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from wattmosaic_engine.api.routes import events as event_routes
from wattmosaic_engine.api.server import FACADE_KEY, NullFacade
from wattmosaic_engine.contracts import (
    Bounds,
    ConfidenceVectorV3,
    DeviceEstimate,
    EstimateStatus,
    EventGroupStatus,
    EventInferenceRecord,
    IdentifiabilityStatus,
    InferenceMethod,
    JointInferenceResult,
    ReconciliationV3,
    UnknownEstimate,
    VersionTriple,
)
from wattmosaic_engine.core.snapshot import AssembleInputs, SnapshotAssembler
from wattmosaic_engine.inference.joint import solve_joint_detailed
from wattmosaic_engine.inference.orchestrator import _unknown_estimates
from wattmosaic_engine.runtime.interfaces import ObservationOperator
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.repositories.inference import repair_point_bounds

from tests.support.fakes import FakeMapping, FakeTopology, fake_operator

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)


# --------------------------------------------------------------------------- #
# 1. Unknown-scope null semantics from the solvers.
# --------------------------------------------------------------------------- #


def _operator_with_unknown_scopes() -> ObservationOperator:
    """Two readers with an unknown column each; one carries no usable sigma."""
    topology = FakeTopology.from_tree({"Main": {"Circuit A": {}}})
    mapping = FakeMapping.from_pairs({("device", "lamp"): "Circuit A"})
    base = fake_operator(
        topology,
        mapping,
        ("Main", "Circuit A"),
        (("device", "lamp"),),
        at=T0,
    )
    return ObservationOperator(
        H=base.H,
        rows=base.rows,
        subjects=base.subjects,
        entries=base.entries,
        unknown_scopes=(("Main", (0,)), ("Circuit A", (1,))),
        soft_alternatives=base.soft_alternatives,
        topology_version_id=base.topology_version_id,
        mapping_version_id=base.mapping_version_id,
        at=base.at,
    )


def test_joint_report_exposes_only_scopes_with_a_solved_row() -> None:
    operator = _operator_with_unknown_scopes()
    # Circuit A has a usable sigma and is solved; Main has no sigma and is
    # excluded (the missing y also excludes it).
    report = solve_joint_detailed(
        operator,
        {"Main": 45.0, "Circuit A": 45.0},
        {"Circuit A": 2.0},
    )
    assert "Circuit A" in report.constrained_unknown_scopes
    assert "Main" not in report.constrained_unknown_scopes
    assert set(report.unknown_scopes) == {"Main", "Circuit A"}


def test_unconstrained_scope_is_published_as_none_not_zero() -> None:
    operator = _operator_with_unknown_scopes()
    u = np.array([45.0, 12.0])
    estimates = _unknown_estimates(
        operator, (), ("Main", "Circuit A"), u, frozenset({"Circuit A"})
    )
    by_scope = {u.scope_reader_id: u.delta_w for u in estimates}
    assert by_scope["Main"] is None  # no solved row constrains it
    assert by_scope["Circuit A"] == pytest.approx(12.0)
    # No published unknown estimate may carry a fabricated 0.0 for an
    # unconstrained scope.
    assert all(u.delta_w != 0.0 for u in estimates if u.scope_reader_id == "Main")


def test_unconstrained_scope_never_emits_zero_from_the_solver_u() -> None:
    """When no row is active at all, every scope stays absent (HB §18)."""
    operator = _operator_with_unknown_scopes()
    report = solve_joint_detailed(operator, {}, {})
    assert report.constrained_unknown_scopes == frozenset()
    estimates = _unknown_estimates(
        operator,
        (),
        report.unknown_scopes,
        report.solution.u,
        report.constrained_unknown_scopes,
    )
    assert all(estimate.delta_w is None for estimate in estimates)


def test_constrained_scope_with_a_real_zero_w_change_reports_zero() -> None:
    """A real 0 W solve on a constrained scope is a measured 0, not absent."""
    operator = _operator_with_unknown_scopes()
    # Both rows active with zero observed change: the solve legitimately yields
    # 0.0 for the constrained scope, and that zero must be preserved.
    report = solve_joint_detailed(
        operator,
        {"Main": 0.0, "Circuit A": 0.0},
        {"Main": 2.0, "Circuit A": 2.0},
    )
    assert "Circuit A" in report.constrained_unknown_scopes
    estimates = _unknown_estimates(
        operator,
        (),
        report.unknown_scopes,
        report.solution.u,
        report.constrained_unknown_scopes,
    )
    constrained = next(u for u in estimates if u.scope_reader_id == "Circuit A")
    assert constrained.delta_w is not None
    assert constrained.delta_w == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# 2. Snapshot assembler: unknown_w sum / None / empty rules.
# --------------------------------------------------------------------------- #


def _result(unknowns: list[UnknownEstimate]) -> JointInferenceResult:
    return JointInferenceResult(
        event_group_id="g",
        status=EventGroupStatus.CONFIRMED,
        device_estimates=[],
        group_estimates=[],
        unknown_estimates=unknowns,
        candidates=[],
        matches={},
        residual_w_by_reader={"Main": None},
        versions=VT,
        computed_at=T0,
    )


def _assembled_unknown_w(unknowns: list[UnknownEstimate]) -> float | None:
    assembler = SnapshotAssembler(SimpleNamespace(put_snapshot=lambda _s: True), lambda: T0)
    result = _result(unknowns)
    snapshot = assembler.on_result(
        result,
        inputs=AssembleInputs(result=result, root_reader_ids=("Main",)),
    )
    return snapshot.reconciliation.unknown_w


def test_assembler_unknown_w_is_zero_over_an_empty_set() -> None:
    assert _assembled_unknown_w([]) == 0.0


def test_assembler_unknown_w_sums_every_present_estimate() -> None:
    unknowns = [
        UnknownEstimate(scope_reader_id="a", point_w=10.0),
        UnknownEstimate(scope_reader_id="b", delta_w=-2.5),
    ]
    assert _assembled_unknown_w(unknowns) == pytest.approx(7.5)


def test_assembler_unknown_w_is_none_when_any_estimate_is_unconstrained() -> None:
    unknowns = [
        UnknownEstimate(scope_reader_id="a", point_w=10.0),
        UnknownEstimate(scope_reader_id="b", delta_w=None),  # unconstrained
    ]
    assert _assembled_unknown_w(unknowns) is None


def test_reconciliation_balance_error_is_computable_when_all_terms_present() -> None:
    recon = ReconciliationV3(
        main_w=100.0,
        identified_w=40.0,
        group_w=10.0,
        unknown_w=20.0,
        residual_w=25.0,
    )
    assert recon.balance_error_w == pytest.approx(5.0)
    # One absent term makes the balance unknown, never a fabricated zero.
    recon.unknown_w = None
    assert recon.balance_error_w is None


# --------------------------------------------------------------------------- #
# 3. Zero-width bounds survive a storage round-trip (shared repair helper).
# --------------------------------------------------------------------------- #


def _point_estimate() -> DeviceEstimate:
    # Same pattern I5 uses: the point bound cannot survive nested Pydantic
    # validation, so construct with a placeholder and attach the real bounds
    # afterwards (assignment validation is off on this model).
    estimate = DeviceEstimate(
        subject_kind="device",
        subject_id="dev-exact",
        point_w=45.0,
        bounds=Bounds(basis="posterior"),
        status=EstimateStatus.MEASURED,
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        method=InferenceMethod.DIRECT_READING,
        confidence=ConfidenceVectorV3(),
        source_reader_ids=["ct_plug"],
        evidence_refs=[],
        versions=VT,
        updated_at=T0,
    )
    estimate.bounds = Bounds._point(45.0, "posterior")
    return estimate


def _record_with_exact_bounds() -> EventInferenceRecord:
    result = JointInferenceResult(
        event_group_id="grp-exact",
        event_group_revision=1,
        status=EventGroupStatus.CONFIRMED,
        device_estimates=[_point_estimate()],
        group_estimates=[],
        unknown_estimates=[],
        candidates=[],
        matches={},
        residual_w_by_reader={"ct_plug": 0.0},
        versions=VT,
        computed_at=T0,
    )
    return EventInferenceRecord(
        record_id="rec-exact",
        event_group_id="grp-exact",
        interpretation_revision=1,
        previous_record_id=None,
        result=result,
        reason="initial",
        created_at=T0,
    )


def test_stored_exact_bounds_round_trip_through_records_for_group(tmp_path) -> None:
    db = Database(tmp_path / "exact.db")
    try:
        record = _record_with_exact_bounds()
        db.repos.evidence.put_record(record)
        rows = db.repos.evidence.records_for_group("grp-exact")
        assert len(rows) == 1
        bounds = rows[0].result.device_estimates[0].bounds
        assert bounds.lower_w == pytest.approx(45.0)
        assert bounds.upper_w == pytest.approx(45.0)
        assert bounds.basis == "posterior"
    finally:
        db.close()


def test_stored_exact_bounds_round_trip_through_event_route(tmp_path) -> None:
    db = Database(tmp_path / "exact-route.db")
    try:
        record = _record_with_exact_bounds()
        db.repos.evidence.put_record(record)

        class _Facade(NullFacade):
            def v3_event(self, group_id):
                rows = db.repos.evidence.records_for_group(group_id)
                if not rows:
                    return None
                return {
                    "group_id": group_id,
                    "records": [r.model_dump(mode="json") for r in rows],
                }

        app = web.Application()
        app[FACADE_KEY] = _Facade()
        request = make_mocked_request(
            "GET",
            "/api/v1/events/grp-exact",
            app=app,
            match_info={"group_id": "grp-exact"},
        )
        response = asyncio.run(event_routes.event(request))
        assert response.status == 200
        body = json.loads(response.text)
        bounds = body["records"][0]["result"]["device_estimates"][0]["bounds"]
        assert bounds["lower_w"] == pytest.approx(45.0)
        assert bounds["upper_w"] == pytest.approx(45.0)
    finally:
        db.close()


def test_shared_repair_gate_allows_point_and_resets_afterwards() -> None:
    point = {"lower_w": 7.0, "upper_w": 7.0, "basis": "posterior"}
    # A point bound from a raw payload is rejected without the trusted repair.
    with pytest.raises(ValueError):
        Bounds(**point)
    # The shared repair rebuilds a point bound as a Bounds instance and opens
    # the gate while the parent validates; the gate resets afterwards.
    payload = {"device_estimates": [{"bounds": dict(point)}]}
    with repair_point_bounds(payload):
        rebuilt = Bounds.model_validate(payload["device_estimates"][0]["bounds"])
    assert rebuilt.lower_w == rebuilt.upper_w == pytest.approx(7.0)
    with pytest.raises(ValueError):
        Bounds(**point)
    # A non-degenerate interval is unaffected.
    wide_payload = {"device_estimates": []}
    with repair_point_bounds(wide_payload):
        wide = Bounds(lower_w=0.0, upper_w=250.0, basis="group_sum")
    assert wide.upper_w == pytest.approx(250.0)
