"""Active-model registry for contract 3.0 (L1).

The v3 :class:`Registry` owns the model lifecycle and the single prediction
path.  It implements :class:`~wattmosaic_engine.runtime.interfaces.ModelSetView`
so inference consumes one deterministic view of the active model set, with a
stable ``model_set_id`` that changes iff the active set changes.

Design rules (from the L1 task / analysis F1, C3, C5, DEF-02, DEF-07):

* **One active version per subject.**  A version is active iff its *last*
  event in the append-only log is ``activated``.  A rejected or never-active
  version can never be active; a rollback reactivates the last version whose
  log shows ``activated`` (DEF-07).
* **Append-only.**  ``propose`` / ``activate`` / ``reject`` / ``rollback`` /
  ``freeze`` / ``unfreeze`` only append rows; they never rewrite a version or
  an event.
* **Atomic swap by version key.**  The active set and its LRU cache are swapped
  together under one lock.  Freeze blocks promotion, never inference.
* **Unknown is None.**  An unknown level/control value yields
  ``PowerPrediction(status=UNKNOWN, mean_w=None)`` — never 0.0.

The legacy :class:`ModelRegistry` is kept below, unchanged, because X03/Wave-0
callers (learning/pipeline, promotion, backend, API) still import it until Z2.
The new code never imports ``runtime.fakes`` and depends only on the
``ModelRepositoryProtocol`` seam in :mod:`models.protocols`.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..contracts import (
    DeltaPrior,
    ModelSetId,
    PowerLevelV3,
    PowerModelEvent,
    PowerModelVersion,
    PowerPrediction,
    PredictionStatus,
)
from ..contracts import model_set_id as make_model_set_id
from ..runtime.interfaces import Clock, ModelSetView, PredictionContext
from .prediction import (
    expected_window_for_spec,
    predict_with_spec,
    transition_prior_for_spec,
)
from .protocols import ModelRepositoryProtocol as ModelRepository
from .serde import version_from_row as _legacy_version_from_row
from .spec import version_from_row_v3

_LOGGER = logging.getLogger(__name__)

#: Events that are audit-only and must not affect active-version derivation.
#: A frozen model is still the active model (freeze blocks promotion, not
#: inference), so a trailing ``frozen``/``unfrozen`` event must not demote it.
_AUDIT_EVENTS = frozenset({"frozen", "unfrozen"})


def _event_sort_key(event: Any) -> tuple[str, int]:
    return (str(event.get("at", "")), int(event.get("id", 0)))


def derive_active_version_id(
    events_by_version: Mapping[str, list[Any]],
) -> str | None:
    """Derive the single active version id from event logs.

    A version is active iff its last *activation-relevant* event (ignoring
    ``frozen``/``unfrozen`` audit events) is ``activated``.  A rejected or
    never-active version is ineligible; a rollback reactivates the last version
    whose log shows ``activated``.  The latest activation wins, so at most one
    version per subject is active.
    """
    eligible: list[tuple[str, str, int]] = []
    for version_id, events in events_by_version.items():
        relevant = [e for e in events if str(e.get("event")) not in _AUDIT_EVENTS]
        if not relevant:
            continue
        last = max(relevant, key=_event_sort_key)
        if str(last.get("event")) != "activated":
            continue
        eligible.append((version_id, str(last.get("at", "")), int(last.get("id", 0))))
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item[1], item[2]))[0]


# --------------------------------------------------------------------------- #
# V3 registry.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ActiveSet:
    """An immutable snapshot of one subject's active model versions."""

    model_set_id: ModelSetId
    versions: Mapping[str, PowerModelVersion] = field(default_factory=dict)


class Registry(ModelSetView):
    """Append-only model lifecycle plus the single v3 prediction path.

    ``clock`` is injected (no wall clock in engine code): every event appended
    by the lifecycle methods is stamped with it, so replay is deterministic.
    """

    def __init__(
        self,
        model_repo: Any,
        clock: Clock | None = None,
        *,
        cache_size: int = 256,
    ) -> None:
        self._repo = model_repo
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._cache_size = max(8, int(cache_size))
        self._cache: OrderedDict[tuple[str, str], PowerPrediction] = OrderedDict()
        self._lock = threading.RLock()
        self._active_set = ActiveSet(model_set_id=make_model_set_id([]))

    # -- helpers ----------------------------------------------------------- #
    def _row_to_version(self, row: Any) -> PowerModelVersion | None:
        if row is None:
            return None
        if isinstance(row, PowerModelVersion):
            return row
        if isinstance(row, dict) and "spec" in row and not isinstance(row.get("spec"), str):
            return version_from_row_v3(row)
        return version_from_row_v3(dict(row))

    def _now(self) -> datetime:
        return self._clock()

    def _version_by_id(self, version_id: str) -> PowerModelVersion | None:
        """Find a version contract by id across every known subject."""
        for subject in self.subjects():
            for row in self._repo.versions(subject):
                if str(row.get("version_id")) == version_id:
                    return self._row_to_version(row)
        return None

    # -- lifecycle (append-only) ------------------------------------------- #
    def propose(
        self,
        version: PowerModelVersion | dict[str, Any],
        *,
        reason: str = "proposed",
    ) -> PowerModelVersion:
        """Append a new candidate version.  Never rewrites an existing row."""
        model = version if isinstance(version, PowerModelVersion) else PowerModelVersion.model_validate(version)
        self._repo.append_version(model)
        self._repo.append_event(
            PowerModelEvent(
                version_id=model.version_id,
                event="candidate",
                reason=reason,
                at=self._now(),
            )
        )
        return model

    def activate(self, version_id: str, *, reason: str = "promoted") -> None:
        """Append an ``activated`` event, superseding the subject's old active.

        A frozen subject refuses promotion (freeze blocks promotion, not
        inference).  A rejected version can never be activated.
        """
        version = self._version_by_id(version_id)
        if version is None:
            raise KeyError(f"unknown model version {version_id!r}")
        if self._last_event(version_id) == "rejected":
            raise ValueError(f"cannot activate rejected version {version_id!r}")
        if self.is_frozen(version.subject_id):
            raise ValueError(f"model for {version.subject_id!r} is frozen")
        old = self._repo.active_version(version.subject_id)
        if old is not None and str(old.get("version_id")) != version_id:
            self._repo.append_event(
                PowerModelEvent(
                    version_id=str(old["version_id"]),
                    event="superseded",
                    reason=reason,
                    at=self._now(),
                )
            )
        self._repo.append_event(
            PowerModelEvent(
                version_id=version_id,
                event="activated",
                reason=reason,
                at=self._now(),
            )
        )
        self.invalidate(version.subject_id)

    def reject(self, version_id: str, *, reason: str = "rejected") -> None:
        """Append a ``rejected`` event (a terminal state)."""
        self._repo.append_event(
            PowerModelEvent(
                version_id=version_id,
                event="rejected",
                reason=reason,
                at=self._now(),
            )
        )

    def rollback(self, subject_id: str, *, reason: str = "rollback") -> str | None:
        """Reactivate the last version whose log shows ``activated``.

        DEF-07: a rejected or never-active version is never a rollback target.
        Only versions whose event log contains a past ``activated`` are
        eligible, and it must currently be superseded/rolled_back (not active).
        Returns the reactivated version id, or ``None`` when there is no
        eligible target.
        """
        candidates: list[tuple[str, datetime, int]] = []
        for row in self._repo.versions(subject_id):
            version_id = str(row.get("version_id"))
            events = self._repo.events(version_id)
            relevant = [e for e in events if str(e.get("event")) not in _AUDIT_EVENTS]
            # Never reactivate a rejected or never-active version (DEF-07).
            if any(str(e.get("event")) == "rejected" for e in relevant):
                continue
            kinds = [str(e.get("event")) for e in relevant]
            if "activated" not in kinds:
                continue
            # Eligible only if it is not currently active.
            if self._last_event(version_id) == "activated":
                continue
            activated = [e for e in relevant if e.get("event") == "activated"]
            last_activation = max(activated, key=_event_sort_key)
            activated_at = self._coerce_time(last_activation.get("at"))
            if activated_at is not None:
                candidates.append((version_id, activated_at, int(last_activation.get("id", 0))))
        if not candidates:
            return None
        target = max(candidates, key=lambda item: (item[1], item[2]))[0]
        # Mark the currently active version rolled_back before reactivating.
        active = self._repo.active_version(subject_id)
        if active is not None:
            self._repo.append_event(
                PowerModelEvent(
                    version_id=str(active["version_id"]),
                    event="rolled_back",
                    reason=reason,
                    at=self._now(),
                )
            )
        self._repo.append_event(
            PowerModelEvent(
                version_id=target,
                event="activated",
                reason=reason,
                at=self._now(),
            )
        )
        self.invalidate(subject_id)
        return target

    def mark_polarity_defect(
        self, subject_id: str, *, reason: str = "polarity_defect"
    ) -> list[str]:
        """DEF-01 follow-up: supersede a version trained on inverted readers.

        P1 confirms the polarity defect; L1 then marks affected versions
        ``superseded`` with this reason.  This is a migration-free event-log
        append (versions and earlier events stay immutable).  A version is
        considered affected when its evidence references name the reader passed
        in ``subject_id`` (the producer supplies that id once confirmed).

        Returns the ids of the versions that were marked.
        """
        marked: list[str] = []
        for row in self._repo.versions(subject_id):
            version = self._row_to_version(row)
            if version is None:
                continue
            if self._last_event(version.version_id) in {"rejected", "superseded"}:
                continue
            self._repo.append_event(
                PowerModelEvent(
                    version_id=version.version_id,
                    event="superseded",
                    reason=reason,
                    at=self._now(),
                )
            )
            marked.append(version.version_id)
        if marked:
            self.invalidate(subject_id)
        return marked

    def freeze(self, subject_id: str, *, reason: str = "frozen") -> None:
        """Freeze a subject.  Promotion is blocked; inference keeps working."""
        self._repo.set_model_frozen(subject_id, True)
        self._append_subject_event(subject_id, "frozen", reason)

    def unfreeze(self, subject_id: str, *, reason: str = "unfrozen") -> None:
        self._repo.set_model_frozen(subject_id, False)
        self._append_subject_event(subject_id, "unfrozen", reason)

    def is_frozen(self, subject_id: str) -> bool:
        try:
            return bool(self._repo.is_model_frozen(subject_id))
        except Exception:  # pragma: no cover - defensive
            return False

    def _append_subject_event(self, subject_id: str, event: str, reason: str) -> None:
        active = self._repo.active_version(subject_id)
        if active is None:
            return
        self._repo.append_event(
            PowerModelEvent(
                version_id=str(active["version_id"]),
                event=event,  # type: ignore[arg-type]
                reason=reason,
                at=self._now(),
            )
        )

    # -- read model -------------------------------------------------------- #
    def subjects(self) -> tuple[str, ...]:
        """All subjects that have at least one version, sorted for stability."""
        # The repository has no subject-list method; derive it from the events
        # table via each version.  Concrete repositories also often expose a
        # `subjects()` helper; prefer it when available.
        helper = getattr(self._repo, "subject_ids", None)
        if callable(helper):
            return tuple(sorted(helper()))
        helper = getattr(self._repo, "subjects", None)
        if callable(helper):
            return tuple(sorted(helper()))
        return ()

    def versions(self, subject_id: str) -> list[PowerModelVersion]:
        out: list[PowerModelVersion] = []
        for row in self._repo.versions(subject_id):
            version = self._row_to_version(row)
            if version is not None:
                out.append(version)
        return out

    def events(self, subject_id: str | None = None, *, version_id: str | None = None) -> list[dict[str, Any]]:
        if version_id is not None:
            return [dict(e) for e in self._repo.events(version_id)]
        if subject_id is None:
            return []
        out: list[dict[str, Any]] = []
        for row in self._repo.versions(subject_id):
            out.extend(dict(e) for e in self._repo.events(str(row.get("version_id"))))
        return sorted(out, key=lambda e: (str(e.get("at")), int(e.get("id", 0))))

    def active_version(self, subject_id: str) -> PowerModelVersion | None:
        # Derive from the event log here so audit events (freeze/unfreeze) never
        # demote an active version; the S18 repository does not yet ignore them.
        by_version: dict[str, list[dict[str, Any]]] = {}
        rows_by_id: dict[str, Any] = {}
        for row in self._repo.versions(subject_id):
            if not isinstance(row, dict):
                continue
            vid = str(row.get("version_id"))
            rows_by_id[vid] = row
            by_version[vid] = self._repo.events(vid)
        winner = derive_active_version_id(by_version)
        if winner is not None:
            version = self._row_to_version(rows_by_id[winner])
            if version is not None:
                return version
        # Fallback for repositories whose event log is not exposed.
        return self._row_to_version(self._repo.active_version(subject_id))

    # -- ModelSetView ------------------------------------------------------ #
    def _build_active_set(self) -> ActiveSet:
        versions: dict[str, PowerModelVersion] = {}
        for subject_id in self.subjects():
            version = self.active_version(subject_id)
            if version is not None:
                versions[subject_id] = version
        return ActiveSet(
            model_set_id=make_model_set_id([v.version_id for v in versions.values()]),
            versions=versions,
        )

    @property
    def model_set_id(self) -> ModelSetId:
        with self._lock:
            return self._active_set.model_set_id

    def active_set(self) -> ModelSetView:
        """Return an immutable :class:`ModelSetView` of the current active set."""
        with self._lock:
            current = self._build_active_set()
            if current.model_set_id != self._active_set.model_set_id or not self._active_set.versions:
                self._active_set = current
            # Return a view bound to this registry so property reads track swaps.
            return self

    def refresh(self) -> ModelSetId:
        """Rebuild the cached active set (call after external repo changes)."""
        with self._lock:
            self._active_set = self._build_active_set()
            return self._active_set.model_set_id

    def predict(self, subject_id: str, ctx: PredictionContext) -> PowerPrediction:
        """Predict one subject's level from its active model (single path)."""
        version = self.active_version(subject_id)
        if version is None:
            level = ctx.level if isinstance(ctx.level, PowerLevelV3) else PowerLevelV3.UNKNOWN
            return PowerPrediction(
                level=level,
                mean_w=None,
                sigma_w=None,
                status=PredictionStatus.UNKNOWN,
                model_version_id=None,
            )
        key = (version.version_id, self._prediction_cache_key(ctx))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        prediction = predict_with_spec(version, ctx)
        with self._lock:
            self._cache[key] = prediction
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return prediction

    @staticmethod
    def _prediction_cache_key(ctx: PredictionContext) -> tuple[Any, ...]:
        attrs = ctx.attributes
        if isinstance(attrs, Mapping):
            frozen = tuple(sorted((str(k), repr(v)) for k, v in attrs.items()))
        else:
            frozen = ()
        return (ctx.level, ctx.transition, frozen)

    def transition_prior(
        self, subject_id: str, transition: str, attrs: Mapping[str, Any]
    ) -> DeltaPrior | None:
        version = self.active_version(subject_id)
        if version is None:
            return None
        return transition_prior_for_spec(version, transition, attrs)

    def expected_window(
        self,
        subject_id: str,
        transition: str,
        attrs: Mapping[str, Any],
        tau_s: Any,
    ) -> tuple[Any, Any] | None:
        version = self.active_version(subject_id)
        if version is None:
            return None
        return expected_window_for_spec(version, transition, attrs, tau_s)

    def invalidate(self, subject_id: str | None = None) -> None:
        with self._lock:
            if subject_id is None:
                self._cache.clear()
                return
            for key in [k for k in self._cache if k[0] in {v.version_id for v in self.versions(subject_id)}]:
                self._cache.pop(key, None)

    # -- internals --------------------------------------------------------- #
    def _last_event(self, version_id: str) -> str | None:
        events = self._repo.events(version_id)
        relevant = [e for e in events if str(e.get("event")) not in _AUDIT_EVENTS]
        if not relevant:
            return None
        return str(max(relevant, key=_event_sort_key).get("event"))

    @staticmethod
    def _coerce_time(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class DatabaseModelRepository:
    """Adapt a storage ``Database`` to :class:`ModelRepository`.

    The S18 ``ModelsRepository`` already owns version rows and the append-only
    event log.  This adapter supplies the two read helpers the registry needs
    but that repository does not expose (``events`` and ``subject_ids``) using
    the same connection.  It never imports runtime fakes.
    """

    def __init__(self, database: Any) -> None:
        self._db = database

    @property
    def _models(self) -> Any:
        return self._db.repos.models

    def append_version(self, version: PowerModelVersion) -> None:
        self._models.append_version(version)

    def append_event(self, event: PowerModelEvent) -> None:
        self._models.append_event(event)

    def active_version(self, subject_id: str) -> dict[str, Any] | None:
        return self._models.active_version(subject_id)

    def versions(self, subject_id: str) -> list[dict[str, Any]]:
        return self._models.versions(subject_id)

    def events(self, version_id: str) -> list[dict[str, Any]]:
        with self._db._lock:  # noqa: SLF001 - storage seam
            rows = self._db._conn.execute(  # noqa: SLF001 - storage seam
                "SELECT * FROM power_model_events WHERE version_id=? ORDER BY at, id",
                (version_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def subject_ids(self) -> list[str]:
        with self._db._lock:  # noqa: SLF001 - storage seam
            rows = self._db._conn.execute(  # noqa: SLF001 - storage seam
                "SELECT DISTINCT subject_id FROM power_model_versions_v3 ORDER BY subject_id"
            ).fetchall()
        return [str(row["subject_id"]) for row in rows]

    def is_model_frozen(self, subject_id: str) -> bool:
        return bool(self._models.is_model_frozen(subject_id))

    def set_model_frozen(self, subject_id: str, frozen: bool) -> None:
        self._models.set_model_frozen(subject_id, frozen)


# --------------------------------------------------------------------------- #
# Legacy registry (unchanged; removed at Z2).
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ModelEstimate:
    """One model-based power estimate with provenance."""

    power_w: float
    confidence: float
    explanation: list[str] = field(default_factory=list)
    stage: str | None = None
    stage_status: Any = None
    model_id: str | None = None
    model_type: Any = None
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
            "stage_status": str(getattr(self.stage_status, "value", self.stage_status)),
            "model_id": self.model_id,
            "model_type": str(getattr(self.model_type, "value", self.model_type)) if self.model_type else None,
            "model_version": self.model_version,
            "sample_count": self.sample_count,
            "uncertainty_w": self.uncertainty_w,
            "frozen": self.frozen,
        }


class ModelRegistry:
    """Legacy active-model registry: DB lookup plus a version-keyed model cache.

    Retained verbatim in behaviour for Wave-0 callers until Z2 deletes it.
    """

    def __init__(self, database: Any, *, cache_size: int = 256) -> None:
        from .base import enum_text  # local to keep import cycles shallow

        self._db = database
        self._enum_text = enum_text
        self._cache: OrderedDict[tuple[str, str, int, str], Any] = OrderedDict()
        self._cache_size = max(8, int(cache_size))
        self._lock = threading.RLock()

    def active(self, device_id: str) -> Any | None:
        row = self._db.active_model(device_id)
        return _legacy_version_from_row(row) if row else None

    def active_row(self, device_id: str) -> dict[str, Any] | None:
        return self._db.active_model(device_id)

    def versions(self, device_id: str) -> list[Any]:
        from .serde import versions_from_rows

        return versions_from_rows(self._db.model_versions(device_id))

    def is_frozen(self, device_id: str) -> bool:
        try:
            return bool(self._db.is_model_frozen(device_id))
        except Exception:  # pragma: no cover - defensive
            return False

    def model_id(self, version: Any) -> str:
        return f"{version.device_id}:{self._enum_text(version.model_type)}"

    def model_for(self, device_id: str) -> Any | None:
        row = self._db.active_model(device_id)
        if not row:
            return None
        from .base import build_model

        try:
            model_type = self._db.model_type_from_row(row) if hasattr(self._db, "model_type_from_row") else None
        except Exception:  # pragma: no cover
            model_type = None
        if model_type is None:
            from ..contracts import ModelType

            try:
                model_type = ModelType(str(row.get("model_type")))
            except ValueError:
                _LOGGER.warning("invalid active model metadata for device %s", device_id)
                return None
        version_number = int(row.get("version", 0))
        status = str(row.get("status") or "")
        key = (device_id, self._enum_text(model_type), version_number, status)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        version = _legacy_version_from_row(row)
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
        with self._lock:
            if device_id is None:
                self._cache.clear()
                return
            for key in [k for k in self._cache if k[0] == device_id]:
                self._cache.pop(key, None)

    def estimate(self, device_id: str, context: Any) -> ModelEstimate | None:
        row = self._db.active_model(device_id)
        if not row:
            return None
        version = _legacy_version_from_row(row)
        if version is None:
            return None
        model = self.model_for(device_id)
        if model is None:
            return None
        from ..contracts import StageStatus

        try:
            if hasattr(model, "predict_detailed"):
                power, stage, stage_status = model.predict_detailed(context)
            else:  # pragma: no cover
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
        if str(version.status) == "frozen" or self.is_frozen(device_id):
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
        row = self._db.active_model(device_id)
        version = _legacy_version_from_row(row) if row else None
        return {
            "device_id": device_id,
            "has_active_model": version is not None,
            "frozen": self.is_frozen(device_id),
            "model_type": self._enum_text(version.model_type) if version else None,
            "version": version.version if version else None,
            "status": version.status if version else None,
            "sample_count": version.sample_count if version else 0,
            "training_quality": version.training_quality if version else 0.0,
            "validation_metrics": dict(version.validation_metrics) if version else {},
            "confidence": version.confidence if version else 0.0,
            "versions": len(self._db.model_versions(device_id)),
        }


__all__ = [
    "ActiveSet",
    "DatabaseModelRepository",
    "ModelEstimate",
    "ModelRegistry",
    "ModelRepository",
    "Registry",
]
