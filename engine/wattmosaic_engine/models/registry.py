"""Active-model registry: cached lookup, instantiation and estimation."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ..contracts import (
    DeviceModelVersion,
    ModelStatus,
    ModelType,
    StageStatus,
)
from ..storage.database import Database
from .base import build_model, enum_text
from .serde import version_from_row

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ModelEstimate:
    """One model-based power estimate with provenance."""

    power_w: float
    confidence: float
    explanation: list[str] = field(default_factory=list)
    stage: str | None = None
    stage_status: StageStatus = StageStatus.UNKNOWN
    model_id: str | None = None
    model_type: ModelType | None = None
    model_version: int | None = None
    sample_count: int = 0
    uncertainty_w: float = 0.0
    frozen: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "power_w": self.power_w,
            "confidence": self.confidence,
            "explanation": list(self.explanation),
            "stage": self.stage,
            "stage_status": enum_text(self.stage_status),
            "model_id": self.model_id,
            "model_type": enum_text(self.model_type) if self.model_type else None,
            "model_version": self.model_version,
            "sample_count": self.sample_count,
            "uncertainty_w": self.uncertainty_w,
            "frozen": self.frozen,
        }


class ModelRegistry:
    """Reads the active model from the database and caches instantiated models.

    Cache invalidation is *version-keyed*, not time-based: every ``active()``
    call performs one indexed database lookup for the current active version and
    reuses the already-parsed model object only when the ``(device_id,
    model_type, version, status)`` tuple is unchanged.  This means an external
    promotion or rollback takes effect on the very next estimate with no TTL
    staleness, while still avoiding repeated JSON parsing in the hot path.

    Corrupt persisted JSON, unknown model types and missing models all return
    ``None`` and log a warning; they never raise into the caller's device loop.
    """

    def __init__(self, database: Database, *, cache_size: int = 256) -> None:
        self._db = database
        self._cache: OrderedDict[tuple[str, str, int, str], Any] = OrderedDict()
        self._cache_size = max(8, int(cache_size))
        self._lock = threading.RLock()

    # -- lookup ----------------------------------------------------------- #
    def active(self, device_id: str) -> DeviceModelVersion | None:
        """Return the active :class:`DeviceModelVersion` or ``None``."""
        row = self._db.active_model(device_id)
        if not row:
            return None
        version = version_from_row(row)
        if version is None:
            _LOGGER.warning("corrupt active model row for device %s; ignoring", device_id)
            return None
        return version

    def active_row(self, device_id: str) -> dict[str, Any] | None:
        return self._db.active_model(device_id)

    def versions(self, device_id: str) -> list[DeviceModelVersion]:
        from .serde import versions_from_rows

        return versions_from_rows(self._db.model_versions(device_id))

    def is_frozen(self, device_id: str) -> bool:
        try:
            return bool(self._db.is_model_frozen(device_id))
        except Exception:  # pragma: no cover - defensive
            return False

    def model_id(self, version: DeviceModelVersion) -> str:
        return f"{version.device_id}:{enum_text(version.model_type)}"

    # -- instantiation ---------------------------------------------------- #
    def model_for(self, device_id: str) -> Any | None:
        """Instantiate (or reuse) the active model object for ``device_id``."""
        row = self._db.active_model(device_id)
        if not row:
            return None
        try:
            model_type = ModelType(str(row.get("model_type")))
            version_number = int(row.get("version"))
        except (TypeError, ValueError):
            _LOGGER.warning("invalid active model metadata for device %s", device_id)
            return None
        status = str(row.get("status") or "")
        key = (device_id, enum_text(model_type), version_number, status)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        version = version_from_row(row)
        if version is None:
            _LOGGER.warning("corrupt model parameters for device %s version %s", device_id, version_number)
            return None
        try:
            model = build_model(
                model_type,
                version.parameters,
                validation_metrics=version.validation_metrics,
                confidence=version.confidence or None,
            )
        except ValueError:
            _LOGGER.warning("unsupported model type %s for device %s", model_type, device_id)
            return None
        with self._lock:
            self._cache[key] = model
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return model

    def invalidate(self, device_id: str | None = None) -> None:
        """Drop cached models (all devices, or one)."""
        with self._lock:
            if device_id is None:
                self._cache.clear()
                return
            for key in [k for k in self._cache if k[0] == device_id]:
                self._cache.pop(key, None)

    # -- estimation ------------------------------------------------------- #
    def estimate(self, device_id: str, context: Any) -> ModelEstimate | None:
        """Estimate device power from the active model.

        Returns ``None`` when no usable model exists.  Frozen models are still
        used for estimation (freezing blocks *promotion*, not inference); the
        ``frozen`` flag is exposed so the API can surface it.
        """
        row = self._db.active_model(device_id)
        if not row:
            return None
        version = version_from_row(row)
        if version is None:
            return None
        model = self.model_for(device_id)
        if model is None:
            return None
        try:
            if hasattr(model, "predict_detailed"):
                power, stage, stage_status = model.predict_detailed(context)
            else:  # pragma: no cover - all concrete models implement it
                power, stage, stage_status = model.predict(context), None, StageStatus.UNKNOWN
            confidence = float(model.confidence(context)) if hasattr(model, "confidence") else 0.0
            uncertainty = float(model.uncertainty_w(context)) if hasattr(model, "uncertainty_w") else 0.0
            explanation = list(model.explanation(context)) if hasattr(model, "explanation") else []
        except Exception as exc:  # isolate per-device failures
            _LOGGER.warning("model prediction failed for device %s: %s", device_id, exc)
            return None
        if power != power:  # NaN guard
            return None
        explanation.append(f"model_version:{version.version}")
        if version.status == ModelStatus.FROZEN.value or self.is_frozen(device_id):
            explanation.append("model_frozen")
        return ModelEstimate(
            power_w=float(power),
            confidence=max(0.0, min(1.0, confidence)),
            explanation=explanation,
            stage=stage,
            stage_status=stage_status if isinstance(stage_status, StageStatus) else StageStatus(stage_status),
            model_id=self.model_id(version),
            model_type=version.model_type,
            model_version=version.version,
            sample_count=version.sample_count,
            uncertainty_w=max(0.0, uncertainty),
            frozen=self.is_frozen(device_id),
        )

    def status(self, device_id: str) -> dict[str, Any]:
        """Compact model status for diagnostics/API."""
        row = self._db.active_model(device_id)
        version = version_from_row(row) if row else None
        return {
            "device_id": device_id,
            "has_active_model": version is not None,
            "frozen": self.is_frozen(device_id),
            "model_type": enum_text(version.model_type) if version else None,
            "version": version.version if version else None,
            "status": version.status if version else None,
            "sample_count": version.sample_count if version else 0,
            "training_quality": version.training_quality if version else 0.0,
            "validation_metrics": dict(version.validation_metrics) if version else {},
            "confidence": version.confidence if version else 0.0,
            "versions": len(self._db.model_versions(device_id)),
        }


__all__ = ["ModelEstimate", "ModelRegistry"]
