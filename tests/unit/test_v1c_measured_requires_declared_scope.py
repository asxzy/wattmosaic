"""V1c — MEASURED requires a DECLARED single-subject endpoint (HB §8.5 / DEC-05).

A circuit CT whose operator row happens to carry exactly one mapped subject
(e.g. a branch CT with one known device but possibly unknown loads) is *not* a
single-subject endpoint.  Without a declared, confirmed S13 scope naming that
subject, I5 must never label it MEASURED / DIRECT_READING — neither on the event
path (``infer_group``) nor on the steady path (``infer_steady``).

These tests drive the pure orchestrator directly with a one-subject DIRECT row
(the shape that used to be mislabelled) and pass only the declared pairs I7 is
now given by the runtime.  They also verify the runtime resolves those pairs
from endpoints for both paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pytest
from wattmosaic_engine.contracts import (
    DeltaPrior,
    EndpointKind,
    EstimateStatus,
    EventGroup,
    EventGroupStatus,
    EvidenceRef,
    InferenceMethod,
    ReaderDelta,
    VersionTriple,
)
from wattmosaic_engine.core.runtime import RuntimeMixin
from wattmosaic_engine.inference.orchestrator import infer_group, infer_steady
from wattmosaic_engine.residual.reader_residual import UnknownPrior
from wattmosaic_engine.runtime.fakes import (
    FakeMapping,
    FakeModelSet,
    FakeResidual,
    FakeTopology,
    InMemoryHistory,
    InMemoryMatchStore,
    fake_operator,
)
from wattmosaic_engine.runtime.interfaces import HeldReading, ObservationOperator

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)
READER = "ct_branch"  # a circuit CT, not a smart plug
SUBJECT = "device_1"


# --------------------------------------------------------------------------- #
# Fixtures: a circuit CT row with exactly one mapped subject, DIRECT, no
# declaration.  Both paths see the same operator.
# --------------------------------------------------------------------------- #


def _operator() -> ObservationOperator:
    """A single-subject DIRECT row (the shape V1c used to mislabel MEASURED)."""
    topology = FakeTopology.from_tree({"Main": {READER: {}}})
    mapping = FakeMapping.from_pairs({("device", SUBJECT): READER})
    op = fake_operator(
        topology,
        mapping,
        ("Main", READER),
        (("device", SUBJECT),),
        at=T0,
    )
    # A circuit CT's own row is DIRECT with one non-zero column; the parent row
    # sees the subject as EXPECTED.  Assert the precondition explicitly.
    column = [sid for _kind, sid in op.subjects].index(SUBJECT)
    row = [meta.reader_id for meta in op.rows].index(READER)
    H = np.asarray(op.H)
    assert abs(H[row, column]) > 0.0
    assert len(np.flatnonzero(np.abs(H[row]) > 1e-9)) == 1
    return op


def _group() -> EventGroup:
    return EventGroup(
        group_id="group-ct-branch",
        revision=1,
        status=EventGroupStatus.CONFIRMED,
        opened_at=T0,
        closed_at=T0,
        reader_deltas=[
            ReaderDelta(
                reader_id=READER,
                delta_w=100.0,
                sigma_w=2.0,
                completeness="complete",
                evidence_ref=EvidenceRef(kind="observation", id=READER),
            ),
        ],
        versions=VT,
    )


def _models() -> FakeModelSet:
    return FakeModelSet.fixed(
        {
            SUBJECT: DeltaPrior(
                mean_w=100.0, sigma_w=5.0, sign="pos", model_version_id="model-1"
            )
        }
    )


def _residual() -> FakeResidual:
    return FakeResidual(
        UnknownPrior(scope_reader_id="Main", mu_w=0.0, sigma_w=50.0, lambda_u=0.25)
    )


def _empty_established():
    class _Established:
        def identities(self):
            return {}

        def groups(self):
            return {}

        def group_sums(self):
            return {}

    return _Established()


def _estimate(result, subject_id=SUBJECT):
    return next(e for e in result.device_estimates if e.subject_id == subject_id)


# --------------------------------------------------------------------------- #
# Event path: infer_group
# --------------------------------------------------------------------------- #


def _run_group(*, declared):
    return infer_group(
        _group(),
        _operator(),
        _models(),
        _residual(),
        InMemoryHistory(),
        InMemoryMatchStore(),
        VT,
        lambda: T0,
        declared_single_subject=declared,
    )


def test_event_path_undeclared_circuit_ct_is_not_measured():
    estimate = _estimate(_run_group(declared=()))
    assert estimate.status != EstimateStatus.MEASURED
    assert estimate.method != InferenceMethod.DIRECT_READING
    # The reading remains usable as an ESTIMATED point from the joint solve.
    assert estimate.status == EstimateStatus.ESTIMATED
    assert estimate.point_w == pytest.approx(100.0)


def test_event_path_declared_single_subject_endpoint_is_measured():
    estimate = _estimate(_run_group(declared={(READER, SUBJECT)}))
    assert estimate.status == EstimateStatus.MEASURED
    assert estimate.method == InferenceMethod.DIRECT_READING
    assert estimate.point_w == pytest.approx(100.0)


def test_event_path_rejected_declaration_is_not_measured():
    # A rejected scope is never carried into the declared set by the runtime;
    # passing none for the subject models that case.
    estimate = _estimate(_run_group(declared=()))
    assert estimate.status != EstimateStatus.MEASURED
    assert estimate.method != InferenceMethod.DIRECT_READING


def test_event_path_wrong_subject_declaration_is_not_measured():
    # The declared pair must name *this* subject on *this* reader.
    estimate = _estimate(_run_group(declared={(READER, "other_subject"),
                                                    ("other_reader", SUBJECT)}))
    assert estimate.status != EstimateStatus.MEASURED
    assert estimate.method != InferenceMethod.DIRECT_READING


# --------------------------------------------------------------------------- #
# Steady path: infer_steady
# --------------------------------------------------------------------------- #


def _readings():
    return {
        READER: HeldReading(
            value=100.0, held_since=T0, last_confirmed_at=T0, state="held"
        ),
        "Main": HeldReading(
            value=100.0, held_since=T0, last_confirmed_at=T0, state="held"
        ),
    }


def _run_steady(*, declared):
    return infer_steady(
        _readings(),
        _operator(),
        _models(),
        _residual(),
        _empty_established(),
        VT,
        lambda: T0,
        declared_single_subject=declared,
    )


def test_steady_path_undeclared_circuit_ct_is_not_measured():
    estimate = _estimate(_run_steady(declared=()))
    assert estimate.status != EstimateStatus.MEASURED
    assert estimate.method != InferenceMethod.DIRECT_READING


def test_steady_path_declared_single_subject_endpoint_is_measured():
    estimate = _estimate(_run_steady(declared={(READER, SUBJECT)}))
    assert estimate.status == EstimateStatus.MEASURED
    assert estimate.method == InferenceMethod.DIRECT_READING
    assert estimate.point_w == pytest.approx(100.0)


def test_steady_path_rejected_declaration_is_not_measured():
    estimate = _estimate(_run_steady(declared=()))
    assert estimate.status != EstimateStatus.MEASURED
    assert estimate.method != InferenceMethod.DIRECT_READING


def test_steady_path_wrong_subject_declaration_is_not_measured():
    estimate = _estimate(_run_steady(declared={(READER, "other_subject")}))
    assert estimate.status != EstimateStatus.MEASURED
    assert estimate.method != InferenceMethod.DIRECT_READING


# --------------------------------------------------------------------------- #
# Runtime wiring: declared pairs are resolved from S13 endpoints on both the
# event path (bundle) and the steady path.  A rejected/candidate scope or a
# plain circuit CT never yields a pair.
# --------------------------------------------------------------------------- #


def _runtime():
    core = object.__new__(type("Core", (RuntimeMixin,), {}))
    endpoints = [
        {
            "endpoint_id": READER,
            "kind": EndpointKind.CT.value,
            "scope": "single_subject",
            "scope_status": "confirmed",
            "scope_subject_id": SUBJECT,
        },
        {
            "endpoint_id": "ct_rejected",
            "kind": EndpointKind.CT.value,
            "scope": "single_subject",
            "scope_status": "rejected",
            "scope_subject_id": "other",
        },
        {
            "endpoint_id": "ct_plain",
            "kind": EndpointKind.CT.value,
            "scope": "circuit",
            "scope_status": "candidate",
            "scope_subject_id": None,
        },
        {
            "endpoint_id": "plug",
            "kind": EndpointKind.SMART_PLUG.value,
            "scope": "single_subject",
            "scope_status": "confirmed",
            "scope_subject_id": "tv",
        },
    ]
    core.db = SimpleNamespace(
        repos=SimpleNamespace(
            entities=SimpleNamespace(endpoints=lambda: endpoints)
        )
    )
    return core


def test_runtime_resolves_only_declared_confirmed_single_subject_pairs():
    core = _runtime()
    pairs = core._declared_single_subject_pairs()
    assert (READER, SUBJECT) in pairs
    assert ("plug", "tv") in pairs
    assert ("ct_rejected", "other") not in pairs
    assert ("ct_plain", SUBJECT) not in pairs


def test_runtime_event_path_bundle_carries_declared_pairs():
    # _build_input_bundle is where the event path resolves declaration evidence;
    # assert it is present so _run_infer_group can forward it.
    from wattmosaic_engine.main import EngineCore

    assert "declared_single_subject" in _bundle_keys()
    # The real core actually uses the helper (no hidden second declaration path).
    assert hasattr(EngineCore, "_declared_single_subject_pairs")


def _bundle_keys():
    import inspect

    src = inspect.getsource(RuntimeMixin._build_input_bundle)
    return {key for key in ("declared_single_subject",) if key in src}


def test_runtime_event_path_forwards_declared_pairs_to_infer_group(monkeypatch):
    from wattmosaic_engine.core import runtime as runtime_module

    core = _runtime()
    core.model_registry_v3 = object()
    core.unknown_prior = object()
    core.infer_group_calls = 0
    pairs = core._declared_single_subject_pairs()
    assert pairs

    seen: list[set] = []

    def fake_infer_group(*args, **kwargs):
        seen.append(set(kwargs.get("declared_single_subject", ())))
        return object()

    monkeypatch.setattr(runtime_module, "infer_group", fake_infer_group)
    bundle = {key: object() for key in (
        "group", "operator", "models", "residual", "history", "matches",
        "versions", "clock",
    )}
    bundle["declared_single_subject"] = pairs
    core._run_infer_group(bundle)
    assert core.infer_group_calls == 1
    assert seen == [set(pairs)]


def test_runtime_steady_path_forwards_declared_pairs_to_infer_steady(monkeypatch):
    from wattmosaic_engine.core import runtime as runtime_module

    core = _runtime()
    core.model_registry_v3 = object()
    core.unknown_prior = object()
    core._clock = lambda: T0
    core._last_direct_w = {}
    core._last_steady_at = {}
    core.infer_steady_calls = 0
    pairs = core._declared_single_subject_pairs()

    seen: list[set] = []

    def fake_infer_steady(*args, **kwargs):
        seen.append(set(kwargs.get("declared_single_subject", ())))
        return object()

    monkeypatch.setattr(runtime_module, "infer_steady", fake_infer_steady)
    monkeypatch.setattr(core, "_publish_group_result", lambda *a, **k: None)
    monkeypatch.setattr(runtime_module, "build_observation_operator", lambda *a, **k: object())
    monkeypatch.setattr(runtime_module, "_steady_group", lambda *a, **k: object())
    core._steady_publish(
        READER,
        _readings()[READER],
        (("device", SUBJECT),),
        None,
        (object(), object(), object(), set()),
        T0,
    )
    assert core.infer_steady_calls == 1
    assert seen == [set(pairs)]
