"""Component-sum model for contract 3.0 (L3).

A physical device can be declared as the sum of user-declared
:class:`~wattmosaic_engine.contracts.LoadComponent` objects (DEC-07).  One HA
entity may drive several components with *separate* kernels — e.g. an HVAC air
handler is a fan plus a compressor, each with its own response delay.  An
HA-entity link targets a component by id (DEC-08), never by a ``component:``
note tag.

Design rules:

* The persisted payload is a :class:`~wattmosaic_engine.contracts.ComponentSumSpec`
  holding the ordered component ids; the L1 codec round-trips it losslessly.
* Each component carries its *own* model version.  A component prediction is
  resolved from the :class:`~wattmosaic_engine.runtime.interfaces.ModelSetView`
  passed in by id — it is never collapsed into a parent-level number.
* **Null policy** — a missing or UNKNOWN component prediction makes the sum
  UNKNOWN (``mean_w=None``), never a partial sum that invents a number for a
  non-identifiable member (HB §8.4).
* **Pure** — no DB, no wall clock, no runtime fakes; only the ModelSetView
  Protocol is required.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..contracts import (
    ComponentSumSpec,
    LoadComponent,
    PowerLevelV3,
    PowerModelVersion,
    PowerPrediction,
    PredictionStatus,
)
from ..runtime.interfaces import ModelSetView, PredictionContext
from .spec import decode_spec, encode_spec

__all__ = [
    "ComponentBinding",
    "ComponentSumModel",
    "component_ids_from_links",
    "predict_component_sum",
]


@dataclass(frozen=True)
class ComponentBinding:
    """One user-declared component bound to its id and device (DEC-07).

    This is a lightweight view over :class:`LoadComponent`; the full entity is
    owned by K01 and persisted by S13.  It is used here only to document that
    components have ids and per-component model versions, not ``component:``
    note tags.
    """

    component_id: str
    device_id: str
    name: str

    @classmethod
    def from_entity(cls, component: LoadComponent) -> ComponentBinding:
        return cls(
            component_id=component.component_id,
            device_id=component.device_id,
            name=component.name,
        )


def component_ids_from_links(links: Sequence[Mapping[str, Any]]) -> list[str]:
    """Collect the distinct component ids targeted by HA-entity links (DEC-08).

    Only links whose ``target_kind`` is ``'component'`` are considered; a device
    link never names a component.  Order follows first appearance.  This replaces
    the retired ``component:`` note-tag convention on the v3 path.
    """
    result: list[str] = []
    for link in links:
        kind = str(getattr(link, "target_kind", None) or link.get("target_kind", ""))
        if kind != "component":
            continue
        cid = getattr(link, "target_id", None)
        if cid is None:
            cid = link.get("target_id")
        if cid is not None and str(cid) not in result:
            result.append(str(cid))
    return result


def _known_mean(prediction: PowerPrediction) -> float | None:
    if prediction.status != PredictionStatus.KNOWN or prediction.mean_w is None:
        return None
    return float(prediction.mean_w)


def predict_component_sum(
    spec: ComponentSumSpec,
    models: ModelSetView,
    ctx: PredictionContext,
) -> PowerPrediction:
    """Sum one prediction per component id, resolved through ``models``.

    Every component must resolve to a KNOWN prediction with a numeric mean;
    otherwise the whole sum is UNKNOWN.  ``sigma_w`` is the root-sum-square of
    the available component sigmas (independent components).  The parent's
    number is never used as a fallback for a missing component.
    """
    means: list[float] = []
    sigmas_sq: list[float] = []
    for component_id in spec.component_ids:
        prediction = models.predict(component_id, ctx)
        mean = _known_mean(prediction)
        if mean is None:
            return PowerPrediction(
                level=ctx.level if ctx.level is not None else PowerLevelV3.UNKNOWN,
                mean_w=None,
                sigma_w=None,
                status=PredictionStatus.UNKNOWN,
                model_version_id=None,
            )
        means.append(mean)
        sigma = prediction.sigma_w
        if sigma is not None:
            sigmas_sq.append(float(sigma) * float(sigma))

    total = float(sum(means))
    sigma_total = math.sqrt(sum(sigmas_sq)) if sigmas_sq else None
    return PowerPrediction(
        level=ctx.level if ctx.level is not None else PowerLevelV3.UNKNOWN,
        mean_w=total,
        sigma_w=sigma_total,
        status=PredictionStatus.KNOWN,
        model_version_id=None,
    )


@dataclass(slots=True)
class ComponentSumModel:
    """A device model that is the sum of its component submodels."""

    spec: ComponentSumSpec
    clock: Any = None

    def __post_init__(self) -> None:
        if self.clock is None:
            self.clock = lambda: datetime.now(UTC)

    @classmethod
    def from_version(cls, version: PowerModelVersion) -> ComponentSumModel:
        if not isinstance(version.spec, ComponentSumSpec):
            raise ValueError("a ComponentSumModel requires a ComponentSumSpec")
        return cls(spec=version.spec)

    @classmethod
    def from_ids(cls, component_ids: Sequence[str]) -> ComponentSumModel:
        return cls(spec=ComponentSumSpec(component_ids=list(component_ids)))

    def predict(self, models: ModelSetView, ctx: PredictionContext) -> PowerPrediction:
        return predict_component_sum(self.spec, models, ctx)

    def encoded(self) -> dict[str, Any]:
        return encode_spec(self.spec)

    @classmethod
    def from_encoded(cls, raw: Any) -> ComponentSumModel:
        spec = decode_spec(raw)
        if not isinstance(spec, ComponentSumSpec):
            raise ValueError("a ComponentSumModel requires a ComponentSumSpec")
        return cls(spec=spec)
