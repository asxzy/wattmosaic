"""L1: lossless v3 power-model codec (DEF-02 regressions).

Every spec kind round-trips byte-for-byte through :mod:`models.spec`, and the
predictions derived from the decoded spec are identical to those from the
original on a context grid.  Two explicit DEF-02 regressions are pinned:

* a component sum keeps its submodels (a rehydrated composite must not predict
  0 W);
* an adjustable model keeps its ``control_variable`` (a ``brightness_pct``
  query is never scaled 2.55x against a ``brightness``-keyed curve).

Reading a newer schema version raises clearly (never a silent default).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    AdjustableSpec,
    ComponentSumSpec,
    DynamicSpec,
    EvidenceRef,
    FixedSpec,
    KernelPhase,
    LevelStat,
    ModelBasis,
    ModelClass,
    PowerLevelV3,
    PowerModelSpec,
    PowerModelVersion,
    PowerPrediction,
    PredictionStatus,
    ResponseKernelSpec,
)
from wattmosaic_engine.models.prediction import predict_with_spec
from wattmosaic_engine.models.serde import SchemaVersionError
from wattmosaic_engine.models.spec import (
    SPEC_SCHEMA_VERSION,
    decode_spec,
    encode_spec,
)
from wattmosaic_engine.runtime.interfaces import PredictionContext

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _stat(median_w: float | None, mad_w: float | None = None, n: int = 5) -> LevelStat:
    return LevelStat(median_w=median_w, mad_w=mad_w, n=n)


def _fixed() -> FixedSpec:
    return FixedSpec(
        levels={
            PowerLevelV3.OFF: _stat(4.0, 0.5),
            PowerLevelV3.STANDBY: _stat(7.0, 1.0),
            PowerLevelV3.ACTIVE: _stat(60.0, 2.0),
        },
        transition_delta={"on": _stat(56.0, 2.0), "off": _stat(-56.0, 2.0)},
    )


def _adjustable() -> AdjustableSpec:
    return AdjustableSpec(
        control_variable="brightness_pct",
        knots_x=[1.0, 50.0, 100.0],
        knots_w=[7.0, 30.0, 60.0],
        monotone=True,
        x_coverage=(1.0, 100.0),
        standby=_stat(7.0, 1.0),
    )


def _dynamic() -> DynamicSpec:
    return DynamicSpec(
        states={"fan": _stat(30.0), "compressor": _stat(150.0)},
        ha_state_to_states={"cool": ["fan", "compressor"]},
        transition_counts={"fan": {"compressor": 3}},
        duration_s={"fan": _stat(120.0)},
    )


def _kernel() -> ResponseKernelSpec:
    return ResponseKernelSpec(
        by_transition={
            "on": [
                KernelPhase(
                    phase="startup",
                    delay_s=_stat(0.5),
                    amplitude_w=_stat(200.0),
                    duration_s=_stat(2.0),
                ),
                KernelPhase(
                    phase="steady",
                    delay_s=_stat(2.5),
                    amplitude_w=_stat(60.0),
                    duration_s=_stat(1000.0),
                ),
            ]
        }
    )


def _components() -> ComponentSumSpec:
    return ComponentSumSpec(component_ids=["lc-fan", "lc-compressor"])


SPECS = [_fixed(), _adjustable(), _dynamic(), _kernel(), _components()]


def _version(spec: PowerModelSpec, n: int) -> PowerModelVersion:
    return PowerModelVersion(
        version_id=f"mdl-dev-{n}",
        subject_kind="device",
        subject_id="dev",
        number=n,
        model_class=ModelClass.FIXED,
        basis=ModelBasis.DELTA,
        spec=spec,
        evidence_refs=[EvidenceRef(kind="event_group", id="g1")],
        trainer_version="test",
        created_at=T0,
    )


def _contexts() -> list[PredictionContext]:
    contexts: list[PredictionContext] = []
    for level in PowerLevelV3:
        for transition in ("on", "off", "mode", ""):
            for attrs in ({}, {"state": "cool"}, {"brightness_pct": 50.0}):
                contexts.append(
                    PredictionContext(
                        at=T0,
                        level=level,
                        transition=transition or None,
                        attributes=attrs,
                    )
                )
    return contexts


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_spec_roundtrip_is_lossless(spec: PowerModelSpec) -> None:
    encoded = encode_spec(spec)
    # The encoded dict is directly JSON serialisable (storage writes it as JSON).
    encoded = json.loads(json.dumps(encoded))
    decoded = decode_spec(encoded, SPEC_SCHEMA_VERSION)
    assert decoded == spec


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.kind)
def test_decoded_spec_predicts_identically_on_a_context_grid(
    spec: PowerModelSpec,
) -> None:
    original = _version(spec, 1)
    restored = _version(decode_spec(encode_spec(spec)), 1)
    components = {
        "lc-fan": predict_with_spec(
            _version(_fixed(), 2),
            PredictionContext(at=T0, level=PowerLevelV3.ACTIVE),
        ),
        "lc-compressor": predict_with_spec(
            _version(_fixed(), 3),
            PredictionContext(at=T0, level=PowerLevelV3.ACTIVE),
        ),
    }
    for ctx in _contexts():
        before = predict_with_spec(original, ctx, component_predictions=components)
        after = predict_with_spec(restored, ctx, component_predictions=components)
        assert before == after


def test_json_string_roundtrip() -> None:
    spec = _adjustable()
    encoded = json.dumps(encode_spec(spec), sort_keys=True)
    assert decode_spec(encoded) == spec


def test_newer_schema_raises_clearly() -> None:
    with pytest.raises(SchemaVersionError, match="newer than this codec"):
        decode_spec(encode_spec(_fixed()), schema_version=SPEC_SCHEMA_VERSION + 1)


def test_corrupt_spec_raises_value_error() -> None:
    with pytest.raises(ValueError):
        decode_spec("{not json")
    with pytest.raises(ValueError):
        decode_spec({"kind": "unknown-kind"})


# --------------------------------------------------------------------------- #
# DEF-02 regressions.
# --------------------------------------------------------------------------- #


def test_component_sum_keeps_its_submodels() -> None:
    """A fitted composite that predicts 100.5 W reloads to the same number."""
    spec = ComponentSumSpec(component_ids=["lc-a", "lc-b"])
    restored = decode_spec(encode_spec(spec))
    assert isinstance(restored, ComponentSumSpec)
    assert restored.component_ids == ["lc-a", "lc-b"]  # not collapsed, not empty

    version = _version(restored, 1)
    prediction = predict_with_spec(
        version,
        PredictionContext(at=T0, level=PowerLevelV3.ACTIVE),
        component_predictions={
            "lc-a": PowerPrediction(
                level=PowerLevelV3.ACTIVE,
                mean_w=40.0,
                status=PredictionStatus.KNOWN,
                sigma_w=None,
                model_version_id="mdl-lc-a-1",
            ),
            "lc-b": PowerPrediction(
                level=PowerLevelV3.ACTIVE,
                mean_w=60.5,
                status=PredictionStatus.KNOWN,
                sigma_w=None,
                model_version_id="mdl-lc-b-1",
            ),
        },
    )
    assert prediction.status == PredictionStatus.KNOWN
    assert prediction.mean_w == pytest.approx(100.5)

    # A missing submodel is not silently treated as 0 W.
    partial = predict_with_spec(
        version,
        PredictionContext(at=T0, level=PowerLevelV3.ACTIVE),
        component_predictions={
            "lc-a": PowerPrediction(
                level=PowerLevelV3.ACTIVE,
                mean_w=40.0,
                status=PredictionStatus.KNOWN,
                sigma_w=None,
                model_version_id="mdl-lc-a-1",
            )
        },
    )
    assert partial.status == PredictionStatus.UNKNOWN
    assert partial.mean_w is None


def test_adjustable_keeps_control_variable() -> None:
    """A `brightness_pct` query is not scaled 2.55x against a pct-keyed curve."""
    spec = _adjustable()
    restored = decode_spec(encode_spec(spec))
    assert isinstance(restored, AdjustableSpec)
    assert restored.control_variable == "brightness_pct"  # DEF-02

    version = _version(restored, 1)
    prediction = predict_with_spec(
        version,
        PredictionContext(at=T0, level=PowerLevelV3.ACTIVE, attributes={"brightness_pct": 50.0}),
    )
    # 50% is an interior knot — if the control variable were lost or renamed to
    # `brightness`, the query would be interpreted as (50/255)*100 and the
    # prediction would be far from 30 W.
    assert prediction.status == PredictionStatus.KNOWN
    assert prediction.mean_w == pytest.approx(30.0)

    # A context with no control value is UNKNOWN (never 0).
    missing = predict_with_spec(
        version,
        PredictionContext(at=T0, level=PowerLevelV3.ACTIVE),
    )
    assert missing.status == PredictionStatus.UNKNOWN
    assert missing.mean_w is None


def test_unknown_level_is_none_never_zero() -> None:
    fixed = _version(_fixed(), 1)
    unknown = predict_with_spec(
        fixed,
        PredictionContext(at=T0, level=PowerLevelV3.TRANSIENT),
    )
    assert unknown.status == PredictionStatus.UNKNOWN
    assert unknown.mean_w is None
    assert unknown.sigma_w is None
