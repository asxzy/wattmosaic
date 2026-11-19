"""K00: HB §18 null policy, applied reflectively to every v3 contract model.

An absent measurement or estimate is ``None``, never ``0``.  A field whose name
ends in ``_w``, ``_kwh``, ``_wh``, ``_a`` or ``_var``, or that is named
``confidence``, may not carry a non-``None`` numeric default.  The test walks
every model exported from the v3 contract submodules, so it automatically
covers K01-K06 models as they land — no per-model edits required.
"""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest
from pydantic import BaseModel
from wattmosaic_engine.contracts import (
    entities,
    foundation,
    inference,
    matrix,
    measurement,
    power_models,
    service,
    topology,
)

V3_MODULES: tuple[ModuleType, ...] = (
    foundation,
    entities,
    measurement,
    topology,
    inference,
    matrix,
    power_models,
    service,
)


def _exported_models(module: ModuleType) -> list[type[BaseModel]]:
    """Every exported name in ``module`` that is a Pydantic model class."""
    models: list[type[BaseModel]] = []
    for name in getattr(module, "__all__", ()):
        obj = getattr(module, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel):
            models.append(obj)
    return models


def _collect_all_models() -> list[type[BaseModel]]:
    collected: list[type[BaseModel]] = []
    for module in V3_MODULES:
        collected.extend(_exported_models(module))
    return collected


@pytest.mark.parametrize("module", V3_MODULES, ids=lambda m: m.__name__)
def test_module_exposes_a_model_registry(module: ModuleType) -> None:
    """Each v3 submodule publishes exports in ``__all__`` for the walk."""
    assert hasattr(module, "__all__")
    assert isinstance(module.__all__, list)


def test_null_policy_walks_every_exported_model() -> None:
    models = _collect_all_models()
    assert models, "no models found — the reflective walk must cover the v3 contracts"
    for model in models:
        violations = foundation.audit_model_null_policy(model)
        assert not violations, f"null-policy violations: {violations}"


def test_gate_detects_a_planted_bad_model() -> None:
    """The gate is real: it flags a planted non-None numeric default."""

    class Bad(BaseModel):
        estimated_power_w: float = 0.0
        confidence: float = 0.5
        energy_kwh: float | None = None
        temperature_c: float = 0.0

    violations = foundation.audit_model_null_policy(Bad)
    assert "Bad.estimated_power_w=0.0" in violations
    assert "Bad.confidence=0.5" in violations
    assert not any("energy_kwh" in v for v in violations)
    assert not any("temperature_c" in v for v in violations)


def test_sensitive_name_detection() -> None:
    for name in ("power_w", "energy_kwh", "energy_wh", "current_a", "reactive_var", "confidence"):
        assert foundation.is_null_sensitive_field_name(name)
    for name in ("device_id", "temperature_c", "count", "duration_s", "name"):
        assert not foundation.is_null_sensitive_field_name(name)


def test_all_v3_modules_are_importable() -> None:
    for name in (
        "foundation",
        "entities",
        "measurement",
        "topology",
        "inference",
        "matrix",
        "power_models",
        "service",
    ):
        importlib.import_module(f"wattmosaic_engine.contracts.{name}")
