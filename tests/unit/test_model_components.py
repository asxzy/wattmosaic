"""L3: Component-sum model.

Covers the task's acceptance points:

* an HVAC fan + compressor sum predicted with separate delays (WP-F3),
* HA-entity links target components (DEC-08), not ``component:`` note tags,
* a missing component yields UNKNOWN, never a partial invented number (HB §8.4),
* the spec round-trips through the L1 codec, and there is no
  ``CompositeDeviceModel`` / ``component:`` tag on the v3 path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from wattmosaic_engine.contracts import (
    ComponentSumSpec,
    DeltaPrior,
    LoadComponent,
    ModelBasis,
    ModelClass,
    PowerLevelV3,
    PowerModelVersion,
    PredictionStatus,
)
from wattmosaic_engine.models.components import (
    ComponentBinding,
    ComponentSumModel,
    component_ids_from_links,
    predict_component_sum,
)
from wattmosaic_engine.models.kernel import KernelModel
from wattmosaic_engine.models.spec import decode_spec, encode_spec
from wattmosaic_engine.runtime.interfaces import PredictionContext

from tests.support.fakes import FakeModelSet
from tools.simulator.hb17 import HVAC_DELAY_S

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _priors() -> dict[str, DeltaPrior]:
    return {
        "lc-fan": DeltaPrior(mean_w=40.0, sigma_w=1.0, sign="pos", model_version_id="mdl-fan-1"),
        "lc-compressor": DeltaPrior(
            mean_w=150.0, sigma_w=2.0, sign="pos", model_version_id="mdl-comp-1"
        ),
    }


def _version(spec: ComponentSumSpec) -> PowerModelVersion:
    return PowerModelVersion(
        version_id="mdl-hvac-1",
        subject_kind="device",
        subject_id="hvac",
        number=1,
        model_class=ModelClass.COMPONENT_SUM,
        basis=ModelBasis.DELTA,
        spec=spec,
        trainer_version="test-L3",
        created_at=T0,
    )


def test_hvac_fan_and_compressor_sum_with_separate_delays() -> None:
    """The fan and compressor each keep their own kernel (0 s vs +90 s)."""
    fan = KernelModel.declared(2.0, 40.0, delay_s=0.0)
    compressor = KernelModel.declared(3.0, 150.0, delay_s=HVAC_DELAY_S)

    # At t=0 only the fan responds; at t=90 both have settled.
    fan_mean, _ = fan.expected_window("on", {}, np.array([0.0]))
    comp_mean, _ = compressor.expected_window("on", {}, np.array([0.0, HVAC_DELAY_S + 1]))
    assert fan_mean[0] == pytest.approx(40.0)
    assert comp_mean[0] == pytest.approx(3.0)  # compressor still in standby
    assert comp_mean[1] == pytest.approx(150.0)

    models = FakeModelSet.fixed(_priors())
    spec = ComponentSumSpec(component_ids=["lc-fan", "lc-compressor"])
    prediction = predict_component_sum(
        spec, models, PredictionContext(at=T0, level=PowerLevelV3.ACTIVE)
    )
    assert prediction.status == PredictionStatus.KNOWN
    assert prediction.mean_w == pytest.approx(190.0)
    assert prediction.sigma_w == pytest.approx((1.0**2 + 2.0**2) ** 0.5)


def test_missing_component_is_unknown_not_partial_sum() -> None:
    models = FakeModelSet.fixed({"lc-fan": _priors()["lc-fan"]})
    spec = ComponentSumSpec(component_ids=["lc-fan", "lc-compressor"])
    prediction = predict_component_sum(
        spec, models, PredictionContext(at=T0, level=PowerLevelV3.ACTIVE)
    )
    assert prediction.status == PredictionStatus.UNKNOWN
    assert prediction.mean_w is None
    assert prediction.sigma_w is None


def test_ha_entity_links_target_components() -> None:
    """Links with target_kind='component' name component ids (DEC-08)."""
    links = [
        {"target_kind": "component", "target_id": "lc-fan"},
        {"target_kind": "component", "target_id": "lc-compressor"},
        {"target_kind": "component", "target_id": "lc-fan"},  # dedup
        {"target_kind": "device", "target_id": "hvac"},
    ]
    assert component_ids_from_links(links) == ["lc-fan", "lc-compressor"]


def test_component_binding_from_entity() -> None:
    component = LoadComponent(
        component_id="lc-fan", device_id="hvac", name="Fan"
    )
    binding = ComponentBinding.from_entity(component)
    assert binding.component_id == "lc-fan"
    assert binding.device_id == "hvac"


def test_component_sum_spec_roundtrips_through_l1_codec() -> None:
    spec = ComponentSumSpec(component_ids=["lc-fan", "lc-compressor"])
    version = _version(spec)
    restored = decode_spec(encode_spec(version.spec))
    assert restored == version.spec
    assert isinstance(restored, ComponentSumSpec)
    assert restored.component_ids == ["lc-fan", "lc-compressor"]

    model = ComponentSumModel.from_version(version)
    assert model.from_encoded(model.encoded()).spec == model.spec


def test_v3_path_has_no_component_note_tags() -> None:
    """The v3 component path uses ids; no ``component:`` note tag is involved."""
    spec = ComponentSumSpec(component_ids=["lc-fan"])
    encoded = encode_spec(spec)
    assert "component:" not in str(encoded)
    assert "notes" not in encoded
