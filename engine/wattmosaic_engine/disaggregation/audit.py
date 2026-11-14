"""One analysis pass over the inference modules, and its versioned audit trail.

Why this module exists
----------------------
Isolation (WP4), association (WP5), circuits (WP6), factors (WP5), residual (WP7)
and hierarchy (WP8) are each correct and each tested in isolation.  Nothing ran
them *together*, in the live process, over the same window, in dependency order --
so on a real install none of their artifacts existed.  This module is the only
place where the order lives:

``HA event → isolation → association → circuits → residual → hierarchy → record``

Design rules
------------
* **Import, never reimplement.**  Every decision (a verdict, a withheld relation,
  a circuit class, a refusal) comes from the owning module.  This file only
  supplies inputs, orders the stages and persists what came back.
* **Nothing is invented on refusal.**  A scope whose residual cannot be computed
  is *reported* as refused with the module's own reason code; it is never given a
  zero residual.  A multi-CT circuit is not summed: the two clamps may be the two
  legs of one appliance, and adding them without evidence would double count.
* **Failure isolation per stage and per item.**  One device, one circuit or one
  relation that raises cannot abort the pass or the inference loop.  The error is
  recorded in ``AuditReport.errors`` under a stable key.
* **Deterministic.**  ``record_id`` is a hash of the identity of the inference
  (device, algorithm version, window instant, revision), and stage outputs are
  ordered before they are written, so the same raw data and the same
  ``algorithm_version`` produce byte-identical records.
* **Append-only.**  A record never overwrites its predecessor: it cites
  ``previous_record_id`` and increments ``revision``.

The pass runs on the existing inference loop (``WattMosaicBackend.process``).  It
starts no thread, no task and no timer of its own.
"""

from __future__ import annotations

import bisect
import contextlib
import hashlib
import json
import math
from collections import Counter, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..contracts import (
    Circuit,
    ConfidenceVector,
    EventIsolation,
    HaEntityBinding,
    InferenceRecord,
    InferenceResult,
    IsolationStatus,
    MeasurementBasis,
    RelationKind,
    ResidualPower,
    Source,
    Topology,
    TopologyRelation,
    TopologyStatus,
    utcnow,
)
from ..device_links import device_for_entity
from ..electrical.factors import FactorRegistry
from ..electrical.hierarchy import HierarchyConfig, HierarchyFinder
from ..electrical.topology import build_graph
from ..models.protocols import window_time
from ..models.residual_power import ResidualAnalyzer, ResidualScope
from ..models.residual_power import persist as persist_residual
from .association import AssociationConfig, AssociationFinder, Observation
from .association import persist as persist_relations
from .circuits import CircuitClassifier, CircuitConfig
from .circuits import persist as persist_circuits
from .isolation import EventIsolator, IsolationConfig, IsolationWindow, SampleCoverage

#: The analysis version stamped onto every artifact this pass writes.  Bumping it
#: is what makes "replay under v2 and diff against v1" a meaningful operation.
DEFAULT_ALGORITHM_VERSION = "disagg@2.0"

#: Bound on events held until their analysis window has elapsed.  A bounded
#: buffer with a counter is honest: the oldest event is dropped and the drop is
#: visible, rather than the buffer growing without limit on a busy install.
DEFAULT_PENDING_EVENTS = 256
#: Per-device inferred-power points, used as the baseline for an event's step.
DEFAULT_POWER_HISTORY = 512
#: Bound on remembered association observations (one per event per CT).
DEFAULT_OBSERVATION_HISTORY = 8192
#: Bound on retained isolation verdicts, so ``/isolation`` describes the last
#: few minutes of analysis instead of only whichever event happened to mature
#: during the most recent cycle (usually none).
DEFAULT_VERDICT_HISTORY = 512

#: Reasons a residual scope was not computed.  Codes, never prose, so they can be
#: counted and asserted on.
REFUSED_MULTI_CT_SCOPE = "multi_ct_scope"
REFUSED_NO_READING = "no_scope_reading"
REFUSED_UNKNOWN_BASIS = "unknown_scope_basis"

#: A record is written when the inference actually moved.  A per-cycle row for
#: every device would append ~640k rows/day on a 37-device install for a value
#: that did not change, so the trail records *changes* plus a heartbeat.
RECORD_POWER_TOLERANCE_W = 1.0
RECORD_CONFIDENCE_TOLERANCE = 0.05
RECORD_HEARTBEAT_S = 3600.0

#: ``AuditReport.counts`` keys, declared once so a reader cannot typo one.
COUNT_KEYS = (
    "events",
    "isolated",
    "contaminated",
    "insufficient_data",
    "observations",
    "associations",
    "overlaps",
    "parent_child",
    "circuits",
    "circuits_classified",
    "residuals",
    "records",
)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AuditConfig:
    """Timings, writing switches and thresholds for one pass.

    ``write_*`` gates only the *storage* of a family.  The algorithms still run,
    because a family that is not written may still be an input to one that is
    (a circuit is classified from associations), and silently changing that
    would make a flag change a computed value.
    """

    analysis_offset_s: float = 0.0
    isolation_window_s: float = 30.0
    power_window_s: float = 60.0
    #: How long an event may wait for its window before it is dropped.  The
    #: tracker retains ``StepTracker.max_samples`` distinct values per channel
    #: (~15 min at the observed cadence); past that the readings a verdict needs
    #: are gone, and analysing it anyway would invent one.
    event_max_age_s: float = 1800.0
    min_baseline_samples: int = 3
    min_event_samples: int = 3
    algorithm_version: str = DEFAULT_ALGORITHM_VERSION
    #: ``device_class label -> timing overrides`` (middle isolation layer).  The
    #: class and per-device layers are what make "one fixed duration is optimal
    #: for every device" false; the global fields above stay the base layer.
    isolation_by_class: Mapping[str, Mapping[str, float]] = field(
        default_factory=dict
    )
    #: ``device_id -> timing overrides`` (strongest isolation layer).
    isolation_by_device: Mapping[str, Mapping[str, float]] = field(
        default_factory=dict
    )
    #: ``device_id -> class labels`` used to look up ``isolation_by_class``.
    isolation_device_classes: Mapping[str, Sequence[str]] = field(
        default_factory=dict
    )
    write_isolation: bool = True
    write_association: bool = True
    write_circuits: bool = True
    write_residual: bool = True
    write_hierarchy: bool = True
    write_records: bool = True

    def __post_init__(self) -> None:
        if float(self.analysis_offset_s) < 0.0:
            raise ValueError("analysis_offset_s must not be negative")
        if float(self.isolation_window_s) <= 0.0:
            raise ValueError("isolation_window_s must be positive")
        if float(self.power_window_s) < float(self.isolation_window_s):
            raise ValueError("power_window_s must be at least isolation_window_s")
        if not str(self.algorithm_version).strip():
            raise ValueError("algorithm_version must not be empty")
        if float(self.event_max_age_s) <= 0.0:
            raise ValueError("event_max_age_s must be positive")
        for name, layers in (
            ("isolation_by_class", self.isolation_by_class),
            ("isolation_by_device", self.isolation_by_device),
        ):
            if not isinstance(layers, Mapping):
                raise ValueError(f"{name} must be a mapping")
            for key, overrides in layers.items():
                if not isinstance(overrides, Mapping):
                    raise ValueError(f"{name}[{key!r}] must be a mapping of timings")
        if not isinstance(self.isolation_device_classes, Mapping):
            raise ValueError("isolation_device_classes must be a mapping")

    @classmethod
    def from_settings(cls, settings: Any) -> AuditConfig:
        """Build from ``EngineConfig`` without importing it (no cycle)."""

        def flag(name: str, default: bool) -> bool:
            raw = getattr(settings, name, default)
            return default if raw is None else bool(raw)

        def layers(name: str) -> dict[str, dict[str, float]]:
            """Timing overrides as floats, dropping values that are not numbers.

            A non-numeric override is a configuration problem that ``validate()``
            reports; the pass must still start, so it keeps the documented
            default for that key instead of raising inside configuration
            projection (where the error would name no culprit).
            """
            raw = getattr(settings, name, {}) or {}
            out: dict[str, dict[str, float]] = {}
            for key, values in dict(raw).items():
                cleaned: dict[str, float] = {}
                for timing, value in dict(values).items():
                    try:
                        cleaned[str(timing)] = float(value)
                    except (TypeError, ValueError):
                        continue
                if cleaned:
                    out[str(key)] = cleaned
            return out

        def classes(name: str) -> dict[str, tuple[str, ...]]:
            raw = getattr(settings, name, {}) or {}
            return {
                str(device): tuple(str(label) for label in labels)
                for device, labels in dict(raw).items()
            }

        enabled = flag("audit_enabled", True)
        return cls(
            analysis_offset_s=float(getattr(settings, "analysis_offset_s", 0.0) or 0.0),
            isolation_window_s=float(
                getattr(settings, "isolation_window_s", 30.0) or 30.0
            ),
            power_window_s=float(
                getattr(settings, "isolation_power_window_s", 60.0) or 60.0
            ),
            algorithm_version=str(
                getattr(settings, "audit_algorithm_version", DEFAULT_ALGORITHM_VERSION)
                or DEFAULT_ALGORITHM_VERSION
            ),
            isolation_by_class=layers("isolation_by_class"),
            isolation_by_device=layers("isolation_by_device"),
            isolation_device_classes=classes("isolation_device_classes"),
            write_isolation=enabled and flag("audit_isolation_enabled", True),
            write_association=enabled and flag("audit_association_enabled", True),
            write_circuits=enabled and flag("audit_circuits_enabled", True),
            write_residual=enabled and flag("audit_residual_enabled", True),
            write_hierarchy=enabled and flag("audit_hierarchy_enabled", True),
            write_records=enabled and flag("audit_records_enabled", True),
        )

    @property
    def isolation_base_window(self) -> IsolationWindow:
        """The global layer every override is applied on top of."""
        return IsolationWindow(
            analysis_offset_s=float(self.analysis_offset_s),
            isolation_window_s=float(self.isolation_window_s),
            power_window_s=float(self.power_window_s),
        )

    @property
    def isolation(self) -> IsolationConfig:
        """Global timings plus the class and per-device override layers.

        Without overrides this is exactly the flat config it used to be; with
        them, ``IsolationConfig.resolve`` actually has the layers its docstring
        promises.  ``IsolationWindow.merged`` drops non-finite/negative values,
        so a typo cannot produce a window that silently never matches.
        """
        base = self.isolation_base_window
        return IsolationConfig(
            analysis_offset_s=base.analysis_offset_s,
            isolation_window_s=base.isolation_window_s,
            power_window_s=base.analysis_window_s,
            min_baseline_samples=int(self.min_baseline_samples),
            min_event_samples=int(self.min_event_samples),
            defaults={
                str(label): IsolationWindow.merged(base, values)
                for label, values in self.isolation_by_class.items()
            },
            per_device={
                str(device): IsolationWindow.merged(base, values)
                for device, values in self.isolation_by_device.items()
            },
        )

    def max_isolation_horizon_s(self) -> float:
        """Longest offset+power window any device can be configured with.

        Events must be held until their *own* window has elapsed.  Using only
        the global window would mature a device with a wider override too early,
        and the verdict would read ``window_not_elapsed`` for a window nobody
        waited for.  With no overrides this is exactly the old value.
        """
        config = self.isolation
        windows = [
            config.global_window,
            *config.defaults.values(),
            *config.per_device.values(),
        ]
        return max(
            float(window.analysis_offset_s) + float(window.analysis_window_s)
            for window in windows
        )

    @property
    def association(self) -> AssociationConfig:
        return AssociationConfig(algorithm_version=f"assoc:{self.algorithm_version}")

    @property
    def circuits(self) -> CircuitConfig:
        return CircuitConfig(algorithm_version=f"circuits:{self.algorithm_version}")

    @property
    def hierarchy(self) -> HierarchyConfig:
        return HierarchyConfig(algorithm_version=f"hierarchy:{self.algorithm_version}")


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class AuditReport:
    """What one pass decided, kept so the API can answer without re-analysing."""

    at: datetime
    mode: str = "incremental"
    isolations: list[EventIsolation] = field(default_factory=list)
    relations: list[TopologyRelation] = field(default_factory=list)
    circuits: list[Circuit] = field(default_factory=list)
    residuals: list[ResidualPower] = field(default_factory=list)
    records: list[InferenceRecord] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNT_KEYS, 0))
    refused: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    written: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "mode": self.mode,
            "counts": dict(self.counts),
            "refused": dict(self.refused),
            "errors": dict(self.errors),
            "stages": {key: dict(value) for key, value in self.stages.items()},
            "written": dict(self.written),
        }

    def isolation_status(self) -> dict[str, Any]:
        """Per-verdict counts plus the individual verdicts of this pass."""
        statuses: Counter[str] = Counter(
            str(getattr(item.status, "value", item.status)) for item in self.isolations
        )
        return {
            "at": self.at.isoformat(),
            "mode": self.mode,
            "source": "last_analysis_pass",
            "algorithm_version": self.stages.get("isolation", {}).get(
                "algorithm_version"
            ),
            "counts": {
                "analysed": len(self.isolations),
                "isolated": int(statuses.get(IsolationStatus.ISOLATED.value, 0)),
                "contaminated": int(statuses.get(IsolationStatus.CONTAMINATED.value, 0)),
                "insufficient_data": int(
                    statuses.get(IsolationStatus.INSUFFICIENT_DATA.value, 0)
                ),
            },
            "verdicts": [
                {
                    "event_id": item.event_id,
                    "device_id": item.device_id,
                    "status": str(getattr(item.status, "value", item.status)),
                    "reason": item.reason,
                    "occurred_at": item.occurred_at.isoformat(),
                    "window_start": item.window_start.isoformat(),
                    "window_end": item.window_end.isoformat(),
                    "competing_device_ids": list(item.competing_device_ids),
                    "power_window_contaminated": item.power_window_contaminated,
                }
                for item in sorted(
                    self.isolations,
                    key=lambda row: (row.occurred_at, row.device_id, row.event_id),
                )
            ],
        }


# --------------------------------------------------------------------------- #
# Series helpers
# --------------------------------------------------------------------------- #
def aligned_series(
    histories: Mapping[str, Sequence[tuple[datetime, float]]],
    *,
    min_points: int = 2,
) -> dict[str, list[float]]:
    """Resample per-channel ``(time, watts)`` points onto one shared time grid.

    A common index is not cosmetic: containment is a pairwise comparison of two
    series, and two arrays of different length cannot be compared at the same
    instant.  The grid is the union of every channel's timestamps and each
    channel is forward-filled (the value at or before that instant), because a
    CT holds its reading until it publishes a new one.  Before a channel's first
    sample the value is ``nan`` -- an absent measurement, which the geometry code
    excludes rather than treating as zero.
    """
    grid = sorted({stamp for rows in histories.values() for stamp, _ in rows})
    if len(grid) < max(2, int(min_points)):
        return {}
    steps = [stamp.timestamp() for stamp in grid]
    out: dict[str, list[float]] = {}
    for ct_id in sorted(histories):
        rows = sorted(histories[ct_id], key=lambda row: row[0])
        if not rows:
            continue
        points = [stamp.timestamp() for stamp, _ in rows]
        values = [float(watts) for _, watts in rows]
        series: list[float] = []
        for moment in steps:
            index = bisect.bisect_right(points, moment) - 1
            series.append(values[index] if index >= 0 else math.nan)
        out[str(ct_id)] = series
    return out


def history_from_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    key_map: Mapping[str, str] | None = None,
) -> dict[str, list[tuple[datetime, float]]]:
    """Turn stored ``ct_samples`` rows into per-channel ``(time, watts)`` points.

    ``ct_samples.ct_id`` is the **aligner** key (the entity object id, e.g.
    ``kitchen_l1_power``), not the topology ``ct_id``.  Reading it as a topology
    id would match no channel and look exactly like "no history yet", so
    ``key_map`` re-keys each row back into the topology namespace; unmapped rows
    are dropped rather than invented into a channel.
    """
    out: dict[str, list[tuple[datetime, float]]] = {}
    for row in rows:
        raw_key = row.get("ct_id")
        if not raw_key:
            continue
        if key_map is not None:
            ct_id = key_map.get(str(raw_key))
            if ct_id is None:
                # A row whose key no channel claims cannot be attributed to a
                # topology id; inventing one would create a channel that does
                # not exist.
                continue
        else:
            ct_id = str(raw_key)
        stamp = _as_datetime(row.get("state_timestamp") or row.get("received_at"))
        raw = row.get("value_w")
        if stamp is None or raw is None:
            continue
        try:
            watts = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(watts):
            continue
        out.setdefault(ct_id, []).append((stamp, watts))
    for points in out.values():
        points.sort(key=lambda item: item[0])
    return out


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _event_row(event: Any) -> dict[str, Any]:
    """One event as a plain row, from an HA event object or a stored row.

    ``DeviceEvent`` carries ``device_id``/``entity_id``/``new_state``/``occurred_at``
    as attributes; the stored ``device_events`` rows carry the same names as keys
    plus ``attributes_json``.  Both must reach the isolator with the same keys, or
    one of the two intake paths silently analyses nothing.
    """
    out: dict[str, Any] = {}
    for name in (
        "event_id",
        "device_id",
        "entity_id",
        "event_type",
        "new_state",
        "previous_state",
        "occurred_at",
        "received_at",
    ):
        value = getattr(event, name, None)
        if value is not None:
            out[name] = value
    attributes = getattr(event, "attributes", None)
    if isinstance(attributes, Mapping):
        out["attributes"] = dict(attributes)
    return out


def _device_class_labels(configuration: Any) -> tuple[str, ...]:
    """Class labels a stored device configuration implies, never guessed.

    ``DeviceType`` is the user-facing appliance class and ``PowerBehavior`` is
    the physical response shape; both are legitimate middle-layer labels, and
    an ``unknown`` on either axis is *not* a class (it is the absence of one).
    Name similarity and HA areas are never consulted.
    """
    labels: list[str] = []
    for name in ("device_type", "behavior"):
        raw = getattr(configuration, name, None)
        text = str(getattr(raw, "value", raw) or "").strip().lower()
        if not text or text == "unknown":
            continue
        if text not in labels:
            labels.append(text)
    return tuple(labels)


def record_id_for(
    device_id: str, algorithm_version: str, at: datetime, revision: int
) -> str:
    """Deterministic identity of one inference, so a replay is byte-identical.

    Only facts about *what was inferred* participate: the device, the algorithm
    version, the window instant and the revision.  A wall-clock timestamp, a
    random UUID or a database row id would make the same replay produce a
    different id, which is exactly what ``J9`` forbids.
    """
    payload = f"{device_id}|{algorithm_version}|{at.isoformat()}|{int(revision)}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"rec_{digest[:24]}"


def persist_records(records: Iterable[InferenceRecord], database: Any) -> int:
    """Append records through ``insert_inference_record``; never an overwrite."""
    count = 0
    for record in records:
        database.insert_inference_record(record)
        count += 1
    return count


def persist_isolations(
    isolations: Iterable[EventIsolation], database: Any, *, algorithm_version: str = ""
) -> int:
    """Store isolation verdicts through ``insert_isolation_verdict``.

    A verdict is what an ``evidence_refs`` entry of ``event:<id>`` points at, so
    keeping it only in process memory made every such reference dangle after a
    restart.  The event id is the identity: a reanalysis replaces the verdict.
    """
    count = 0
    for verdict in isolations:
        database.insert_isolation_verdict(verdict, algorithm_version=algorithm_version)
        count += 1
    return count


def _confidence_delta(
    previous: ConfidenceVector | None, current: ConfidenceVector
) -> dict[str, float]:
    """Only the dimensions that moved, to full recorded precision."""
    if previous is None:
        return {}
    out: dict[str, float] = {}
    for name, value in current.dimensions().items():
        before = getattr(previous, name, None)
        if before is None:
            continue
        delta = round(float(value) - float(before), 6)
        if delta:
            out[name] = delta
    return out


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #
class AuditPass:
    """Runs the six inference modules in dependency order over one window."""

    def __init__(
        self,
        database: Any | None = None,
        *,
        config: AuditConfig | None = None,
        tracker: Any | None = None,
        bindings: Iterable[HaEntityBinding] | None = None,
    ) -> None:
        self.config = config or AuditConfig()
        self.database = database
        self.tracker = tracker
        self.last_report: AuditReport | None = None
        #: Entity bindings the isolator uses to collapse many HA entities of one
        #: appliance into one physical device.  ``None`` means "read the stored
        #: declaration"; an explicit list (including ``[]``) overrides it.  An
        #: unbound entity still counts as load evidence, so an install with no
        #: declaration keeps exactly the conservative behaviour it had.
        self._explicit_bindings: list[HaEntityBinding] | None = (
            None if bindings is None else list(bindings)
        )
        self._bindings: list[HaEntityBinding] = list(self._explicit_bindings or ())
        #: ``device_id -> class labels`` for the middle isolation layer.  Read
        #: from the stored device configuration beside the bindings, so an
        #: install that types its devices gets the class layer with no extra
        #: configuration.
        self._device_classes: dict[str, tuple[str, ...]] = {}
        self._load_declarations()
        #: Last inferred power per device, so the next event has a step to
        #: measure.  Seeded from the newest stored record at construction, which
        #: is what makes a restart continue the same chain instead of starting
        #: every device at revision 1 with no delta.
        self._last_power: dict[str, float] = {}
        self._last_revision: dict[str, int] = {}
        self._last_record: dict[str, str] = {}
        self._last_confidence: dict[str, ConfidenceVector] = {}
        #: Set only for the duration of a stored-history pass (see ``reanalyze``).
        self._override_histories: (
            dict[str, list[tuple[datetime, float]]] | None
        ) = None
        #: Events not yet analysed because their power window has not elapsed.
        #: ``ISOLATED`` is a claim about a window, so an event noticed this cycle
        #: cannot support it: the tracker will hold the post-event readings only
        #: after they happen.  Holding the event here is what lets the live pass
        #: reach a verdict at all -- analysing it on arrival produces
        #: ``insufficient_data`` every time, forever.
        self._pending_events: deque[dict[str, Any]] = deque(
            maxlen=DEFAULT_PENDING_EVENTS
        )
        #: Events dropped because they were older than ``event_max_age_s`` and
        #: the readings they needed had left the retained horizon.  Counted so a
        #: permanently-full queue is visible instead of silently lossy.
        self.events_expired = 0
        #: Per-device inferred-power points, so an event's step can be measured
        #: against the value *at the event* rather than the previous cycle's.
        self._power_history: dict[str, deque[tuple[datetime, float]]] = {}
        #: Association evidence, kept across passes so ``min_events`` (>= 2) is
        #: reachable at all.  Indexed by (device, event, CT) so re-observing the
        #: same event cannot count twice.
        self._observation_index: dict[
            tuple[str, str, str | None], Observation
        ] = {}
        self._observation_order: deque[tuple[str, str, str | None]] = deque(
            maxlen=DEFAULT_OBSERVATION_HISTORY
        )
        #: Isolation verdicts kept across passes, keyed by event id so
        #: re-analysing an event replaces its verdict instead of adding one.
        #: The live pass matures a handful of events per hour, so reporting only
        #: the most recent cycle's verdicts would answer "0 analysed" almost
        #: always -- indistinguishable from an analysis that never ran.
        self._verdicts: dict[str, EventIsolation] = {}
        self._verdict_order: deque[str] = deque(maxlen=DEFAULT_VERDICT_HISTORY)
        self._seed_from_database()

    # -- lifecycle -------------------------------------------------------- #
    def _load_declarations(self) -> None:
        """Read the stored entity bindings and device classes.

        ``ha_entity_bindings`` (migration 9) is a projection of
        ``DeviceConfiguration.entity_bindings``, so reading the configurations
        themselves is the source of truth and also yields the device type used
        for the class layer.  A read failure is not fatal: the pass then behaves
        exactly as it did before bindings existed (every entity is a possible
        load change, every device keeps the global window), which is the
        conservative answer rather than a silent guess.
        """
        self._device_classes = {
            str(device): tuple(str(label) for label in labels)
            for device, labels in self.config.isolation_device_classes.items()
        }
        if self._explicit_bindings is not None:
            return
        self._bindings = []
        database = self.database
        if database is None:
            return
        load = getattr(database, "load_device_configurations", None)
        if not callable(load):
            return
        try:
            configurations = load()
        except Exception:  # noqa: BLE001 - a corrupt store is not fatal
            return
        bindings: list[HaEntityBinding] = []
        configured: dict[str, tuple[str, ...]] = {
            str(device): tuple(labels)
            for device, labels in self.config.isolation_device_classes.items()
        }
        for configuration in getattr(configurations, "devices", ()) or ():
            device_id = str(getattr(configuration, "device_id", "") or "")
            if not device_id:
                continue
            for binding in getattr(configuration, "entity_bindings", ()) or ():
                bindings.append(binding)
            labels = _device_class_labels(configuration)
            if labels:
                # The operator's explicit labels come first, then the stored
                # type/behaviour, so a hand-written label wins a collision.
                existing = configured.get(device_id, ())
                configured[device_id] = tuple(dict.fromkeys([*existing, *labels]))
        self._bindings = bindings
        self._device_classes = configured

    def refresh_declarations(self) -> None:
        """Re-read the stored declarations (after a configuration write).

        Bindings and device types are user declarations, not observations: they
        can change while the process runs.  Re-reading on demand keeps a long
        lived engine from analysing with a role the user has already corrected.
        """
        self._load_declarations()

    def _seed_from_database(self) -> None:
        """Continue the revision chain across a restart.

        Without this, the first pass after every restart would write revision 1
        with no ``previous_record_id``, and the audit trail would show a
        brand-new device every time the process came up.
        """
        database = self.database
        if database is None:
            return
        try:
            rows = database.latest_inference_results()
        except Exception:  # noqa: BLE001 - history is an optimisation, not truth
            rows = []
        for row in rows:
            device_id = str(row.get("device_id") or "")
            if not device_id:
                continue
            try:
                self._last_power[device_id] = float(row["power_w"])
            except (KeyError, TypeError, ValueError):
                continue
        for device_id in list(self._last_power):
            previous = self._previous(device_id)
            if previous is None:
                continue
            with contextlib.suppress(TypeError, ValueError):
                self._last_revision[device_id] = int(previous.get("revision") or 1)
            record_id = previous.get("record_id")
            if record_id:
                self._last_record[device_id] = str(record_id)
            confidence = _coerce_confidence(previous.get("confidence"))
            if confidence is not None:
                self._last_confidence[device_id] = confidence

    def _previous(self, device_id: str) -> dict[str, Any] | None:
        """The newest stored record for a device, if storage is available."""
        database = self.database
        if database is None:
            return None
        try:
            return database.latest_inference_record(device_id)
        except Exception:  # noqa: BLE001 - a read failure must not stop the pass
            return None

    # -- entry points ----------------------------------------------------- #
    def analyze(
        self,
        window: Any,
        *,
        topology: Topology,
        results: Sequence[InferenceResult] = (),
        events: Sequence[Any] | None = None,
        now: datetime | None = None,
        mode: str = "incremental",
    ) -> AuditReport:
        """One pass over ``window``.  Never raises for a single bad input."""
        at = now or window_time(window)
        report = AuditReport(at=at, mode=mode)
        # Declarations are user knowledge and can be corrected while the process
        # runs, so they are re-read per pass rather than frozen at construction.
        # An explicit ``bindings=`` argument is never overridden.
        if self._explicit_bindings is None and self.database is not None:
            self._load_declarations()
        graph = self._graph(topology, report)
        if graph is None:
            self.last_report = report
            return report

        registry = self._registry(topology, report)
        by_device = {str(item.device_id): item for item in results}
        for device_id, result in by_device.items():
            with contextlib.suppress(TypeError, ValueError):
                self._record_power(device_id, at, float(result.power_w))
        histories = self._histories(window, report)
        readings = self.observe_readings(window)

        source_events = list(events) if events is not None else self._window_events(window)
        ready, waiting = self._mature_events(source_events, at, mode)
        if mode != "reanalysis":
            # Reported even when zero: "nothing is waiting" is the answer to
            # "is the hold draining?", and a missing key cannot say that.
            report.stages["pending_events"] = {"waiting": waiting}
        isolations = self._isolate(ready, topology, histories, at, report)
        relations = self._associate(
            isolations, histories, by_device, registry, graph, at, report
        )
        residual_relations = self._hierarchy(relations, graph, histories, registry, at, report)
        if residual_relations:
            relations = [*relations, *residual_relations]

        circuits = self._circuits(
            relations, registry, graph, by_device, readings, at, report
        )
        records = self._records(by_device, isolations, relations, circuits, topology, at, report)

        report.isolations = isolations
        report.relations = relations
        report.circuits = circuits
        report.records = records
        self.last_report = report
        return report

    def reanalyze(
        self,
        *,
        topology: Topology,
        lookback_s: float,
        now: datetime | None = None,
        results: Sequence[InferenceResult] = (),
    ) -> AuditReport:
        """Re-derive every artifact from stored raw history.

        This is the pass that can reach a verdict for events that were too fresh
        when they arrived: ``ISOLATED`` requires the analysis window to have
        elapsed, and an event noticed this cycle has not.
        """
        at = now or utcnow()
        report = AuditReport(at=at, mode="reanalysis")
        database = self.database
        graph = self._graph(topology, report)
        if database is None or graph is None:
            self.last_report = report
            return report

        start = at - timedelta(seconds=max(60.0, float(lookback_s)))
        rows: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        # ``ct_samples`` is keyed by the *aligner* key -- the entity object id
        # (``sensor.kitchen_l1_power`` → ``kitchen_l1_power``) -- while every
        # algorithm here speaks topology ``ct_id``.  Querying the topology id
        # returns nothing at all, which is indistinguishable from "no history".
        key_map = self._aligner_keys(topology)
        keys = sorted(key_map)
        try:
            rows = database.ct_samples_between(start, at, keys)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            report.errors["ct_samples_between"] = f"{type(exc).__name__}: {exc}"
        try:
            events = list(database.recent_device_events(start, limit=2000))
        except Exception as exc:  # noqa: BLE001
            report.errors["recent_device_events"] = f"{type(exc).__name__}: {exc}"

        histories = history_from_rows(rows, key_map=key_map)
        self._seed_power_history(topology, start, at)
        # A stored-history pass replaces the tracker as the source of `_histories`
        # for the duration of one call.  It is not a mode switch: the live pass
        # keeps using the tracker.
        self._override_histories = histories
        try:
            return self.analyze(
                _StoredWindow(at=at, events=events, histories=histories),
                topology=topology,
                results=results,
                events=events,
                now=at,
                mode="reanalysis",
            )
        finally:
            self._override_histories = None

    # -- stage 0: inputs --------------------------------------------------- #
    @staticmethod
    def _aligner_keys(topology: Topology) -> dict[str, str]:
        """``aligner key -> topology ct_id`` for every enabled channel.

        Same derivation the ingestion path uses
        (``entity_id.split(".", 1)[-1]``); a topology id is never assumed to be
        the storage key.
        """
        out: dict[str, str] = {}
        for channel in topology.channels:
            if not getattr(channel, "enabled", True):
                continue
            entity_id = str(getattr(channel, "entity_id", "") or "")
            if not entity_id:
                continue
            key = entity_id.split(".", 1)[-1] if "." in entity_id else entity_id
            out[key] = str(channel.ct_id)
        return out

    def _graph(self, topology: Topology, report: AuditReport) -> Any | None:
        try:
            return build_graph(topology)
        except Exception as exc:  # noqa: BLE001
            report.errors["build_graph"] = f"{type(exc).__name__}: {exc}"
            return None

    def _registry(self, topology: Topology, report: AuditReport) -> FactorRegistry:
        """Factor intervals for every channel, or an empty registry.

        An empty registry answers ``CT_MEASURED`` for a channel with no history,
        which is the documented meaning of "no factor was ever declared" -- not
        an assumption that a factor exists.
        """
        if self.database is None:
            return FactorRegistry()
        try:
            registry = FactorRegistry.from_database(
                self.database, [channel.ct_id for channel in topology.channels]
            )
        except Exception as exc:  # noqa: BLE001 - a corrupt factor store is reported
            report.errors["factors"] = f"{type(exc).__name__}: {exc}"
            return FactorRegistry()
        report.stages["factors"] = {
            "channels_with_history": sum(
                1 for channel in topology.channels if registry.has_history(channel.ct_id)
            )
        }
        return registry

    def _histories(self, window: Any, report: AuditReport) -> dict[str, list[tuple[datetime, float]]]:
        """Per-channel sample history for this window.

        The live window is a point-in-time snapshot, so the only history
        available is the backend's own ``StepTracker`` (the same buffer CT step
        detection uses).  Using it keeps the analysis and the solver looking at
        the same evidence instead of two different pictures of the past.  A
        stored-history pass supplies its own, already-loaded series.
        """
        if self._override_histories is not None:
            stored = {
                str(ct_id): list(points)
                for ct_id, points in self._override_histories.items()
            }
            report.stages["series"] = {
                "channels": len(stored),
                "points": sum(len(points) for points in stored.values()),
                "source": "stored_samples",
            }
            return stored
        histories: dict[str, list[tuple[datetime, float]]] = {}
        tracker = self.tracker
        readings = getattr(window, "readings", None)
        channels: list[str] = []
        if isinstance(readings, Mapping):
            channels = [str(key) for key in readings]
        elif isinstance(getattr(window, "channels", None), Mapping):
            channels = [str(key) for key in window.channels]
        if tracker is not None:
            for ct_id in channels:
                try:
                    points = list(tracker.history(ct_id))
                except Exception:  # noqa: BLE001 - a missing channel is not an error
                    continue
                if points:
                    histories[ct_id] = [(stamp, float(watts)) for stamp, watts in points]
        if histories:
            report.stages["series"] = {
                "channels": len(histories),
                "points": sum(len(points) for points in histories.values()),
                "source": "tracker",
            }
        return histories

    def _mature_events(
        self, events: Sequence[Any], at: datetime, mode: str
    ) -> tuple[list[Any], int]:
        """Split incoming events into "analysable now" and "not yet".

        An isolation verdict is a statement about a window.  ``ISOLATED`` also
        requires that window to have *elapsed* (``window_not_elapsed`` otherwise),
        and the post-event readings only exist after they happen.  A live pass
        that analysed an event on the cycle it arrived therefore reported
        ``insufficient_data`` for every event, forever -- present in the report,
        never a verdict.

        Events are held in a bounded queue until ``occurred_at + offset + power
        window`` has passed, then analysed.  The stored-history pass
        (``reanalysis``) ignores the queue: it re-derives from raw samples and
        re-checks every event in its lookback, which is what lets a restart
        reach a verdict for an event that matured while the process was down.
        """
        if mode == "reanalysis":
            return list(events), 0
        now = at
        ready: list[Any] = []
        for raw in events:
            row = dict(raw) if isinstance(raw, Mapping) else _event_row(raw)
            occurred_at = _as_datetime(row.get("occurred_at"))
            if occurred_at is None:
                continue
            if row.get("received_at") is None:
                row["received_at"] = occurred_at
            self._pending_events.append(row)
        # Hold for the *longest* configured window, not the global one: an event
        # whose device has a wider override would otherwise be analysed before
        # its own window elapsed and read ``window_not_elapsed`` for a window
        # nobody waited for.  With no overrides this is the old value exactly.
        horizon = max(0.0, float(self.config.isolation.analysis_offset_s)) + float(
            self.config.isolation.power_window_s
        )
        horizon = max(horizon, self.config.max_isolation_horizon_s())
        waiting = 0
        while self._pending_events:
            row = self._pending_events[0]
            occurred_at = _as_datetime(row.get("occurred_at"))
            if occurred_at is not None and (now - occurred_at).total_seconds() < horizon:
                break
            self._pending_events.popleft()
            if occurred_at is None:
                continue
            if (now - occurred_at).total_seconds() > self.config.event_max_age_s:
                # Too old to analyse: the readings it needs are outside the
                # tracker's retained horizon, so a verdict would be invented.
                self.events_expired += 1
                continue
            ready.append(row)
        waiting = len(self._pending_events)
        return ready, waiting

    def _window_events(self, window: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in getattr(window, "state_transitions", ()) or ():
            if isinstance(raw, Mapping):
                out.append(dict(raw))
                continue
            out.append(
                {
                    "device_id": getattr(raw, "device_id", None),
                    "entity_id": getattr(raw, "entity_id", None),
                    "new_state": getattr(raw, "new_state", None),
                    "previous_state": getattr(raw, "previous_state", None),
                    "occurred_at": getattr(raw, "occurred_at", None),
                }
            )
        return out

    def _enrich(self, events: Sequence[Any], topology: Topology) -> list[dict[str, Any]]:
        """Attach the topology's declared CT scope to each event.

        ``EventIsolator`` reads ``attributes["supporting_ct_ids"]`` to tell a
        competitor on a *different* meter from one on the same conductor.  The
        transitions the engine hands over carry no attributes, so without this
        every competitor looks like it shares a CT and cleanliness is
        under-reported.

        Events arrive in two shapes: the engine's ``DeviceEvent`` objects and the
        stored ``device_events`` rows.  Both are normalised here -- reading only
        ``Mapping`` dropped every object-shaped event on the floor, which is the
        shape ``main.py`` actually passes.
        """
        out: list[dict[str, Any]] = []
        for raw in events:
            row = dict(raw) if isinstance(raw, Mapping) else _event_row(raw)
            if not row:
                continue
            # A stored ``device_events`` row keeps ``attributes_json`` as text and
            # ``occurred_at`` as ISO text.  Both are normalised here so the
            # stored-history pass reaches the isolator in exactly the same shape
            # as a live event -- otherwise it analyses nothing and reports a
            # parse error per event.
            if isinstance(row.get("attributes_json"), str) and not row.get("attributes"):
                with contextlib.suppress(ValueError, TypeError):
                    parsed = json.loads(row["attributes_json"] or "{}")
                    if isinstance(parsed, Mapping):
                        row["attributes"] = dict(parsed)
            for name in ("occurred_at", "received_at", "processed_at"):
                if isinstance(row.get(name), str):
                    row[name] = _as_datetime(row[name])
            entity_id = row.get("entity_id")
            link = device_for_entity(topology, entity_id) if entity_id else None
            attributes = dict(row.get("attributes") or {})
            if link is not None:
                device_id, ct_ids = link
                row.setdefault("device_id", device_id)
                attributes.setdefault("supporting_ct_ids", list(ct_ids))
            row["attributes"] = attributes
            if row.get("received_at") is None:
                row["received_at"] = row.get("occurred_at")
            out.append(row)
        return out

    # -- stage 1: isolation ------------------------------------------------ #
    def _isolate(
        self,
        events: Sequence[Any],
        topology: Topology,
        histories: Mapping[str, Sequence[tuple[datetime, float]]],
        at: datetime,
        report: AuditReport,
    ) -> list[EventIsolation]:
        report.stages["isolation"] = {
            "algorithm_version": self.config.algorithm_version,
            "events": len(events),
            "window_s": float(self.config.isolation_window_s),
            "offset_s": float(self.config.analysis_offset_s),
            # Declarations that change the answer, reported so a verdict can be
            # explained without reading the configuration store.
            "bindings": len(self._bindings),
            "device_classes": len(self._device_classes),
            "class_overrides": sorted(self.config.isolation_by_class),
            "device_overrides": sorted(self.config.isolation_by_device),
        }
        if not events:
            return []
        enriched = self._enrich(events, topology)
        isolator = EventIsolator(
            self.config.isolation,
            bindings=self._bindings,
            device_classes=self._device_classes,
        )
        out: list[EventIsolation] = []
        for row in enriched:
            key = f"isolation:{row.get('device_id')}@{row.get('occurred_at')}"
            try:
                occurred_at = _as_datetime(row.get("occurred_at"))
                if occurred_at is None:
                    continue
                coverage = self._coverage(occurred_at, histories, str(row.get("device_id") or ""))
                verdict = isolator.classify(row, enriched, now=at, coverage=coverage)
            except Exception as exc:  # noqa: BLE001 - one event never stops the pass
                report.errors[key] = f"{type(exc).__name__}: {exc}"
                continue
            out.append(verdict)
            self._remember_verdict(verdict)
            report.counts["events"] += 1
            status = _enum_text(verdict.status)
            if status == IsolationStatus.ISOLATED.value:
                report.counts["isolated"] += 1
            elif status == IsolationStatus.CONTAMINATED.value:
                report.counts["contaminated"] += 1
            else:
                report.counts["insufficient_data"] += 1
        if out and self.config.write_isolation:
            # Persisted *before* the record stage runs, so ``event:<id>`` refs
            # are resolvable by the time a record cites them.
            report.written["isolations"] = self._write(
                lambda items, database: persist_isolations(
                    items, database, algorithm_version=self.config.algorithm_version
                ),
                out,
                report,
                "isolations",
            )
        return out

    def _remember_verdict(self, verdict: EventIsolation) -> None:
        """Keep this verdict for the ``/isolation`` answer, deduped by event id.

        A verdict is about an event, so a reanalysis of the same event replaces
        the earlier one.  Without retention the endpoint reports whichever
        handful of events matured in the last cycle -- usually none, which reads
        exactly like an analysis that never ran.
        """
        key = str(verdict.event_id)
        if key not in self._verdicts:
            if len(self._verdict_order) == (self._verdict_order.maxlen or 0):
                # A bounded deque drops its oldest item on append, so the key is
                # captured *before* the append; otherwise it would stay in the
                # dict forever with nothing pointing at it.
                dropped = self._verdict_order[0]
                self._verdicts.pop(dropped, None)
            self._verdict_order.append(key)
        self._verdicts[key] = verdict

    def retained_verdicts(self) -> list[EventIsolation]:
        """Every retained verdict, oldest first (deterministic order)."""
        return [
            self._verdicts[key] for key in self._verdict_order if key in self._verdicts
        ]

    def isolation_status(self) -> dict[str, Any]:
        """Verdict counts over the retained analysis window, not just the last cycle.

        ``source`` still names the analysis pass (this is what a pass decided);
        ``scope`` says the answer spans retained verdicts rather than only the
        most recent cycle, because the live pass matures events in small groups
        and the last cycle alone would usually read as "0 analysed".
        """
        report = self.last_report
        if report is None:
            return {
                "at": None,
                "mode": None,
                "source": "no_analysis_pass_yet",
                "scope": "retained_verdicts",
                "algorithm_version": self.config.algorithm_version,
                "counts": {
                    "analysed": 0,
                    "isolated": 0,
                    "contaminated": 0,
                    "insufficient_data": 0,
                },
                "verdicts": [],
            }
        verdicts = sorted(
            self.retained_verdicts(),
            key=lambda row: (row.occurred_at, row.device_id, row.event_id),
        )
        statuses: Counter[str] = Counter(_enum_text(item.status) for item in verdicts)
        return {
            "at": report.at.isoformat(),
            "mode": report.mode,
            "source": "last_analysis_pass",
            "scope": "retained_verdicts",
            "algorithm_version": self.config.algorithm_version,
            "counts": {
                "analysed": len(verdicts),
                "isolated": int(statuses.get(IsolationStatus.ISOLATED.value, 0)),
                "contaminated": int(statuses.get(IsolationStatus.CONTAMINATED.value, 0)),
                "insufficient_data": int(
                    statuses.get(IsolationStatus.INSUFFICIENT_DATA.value, 0)
                ),
            },
            "verdicts": [
                {
                    "event_id": item.event_id,
                    "device_id": item.device_id,
                    "status": _enum_text(item.status),
                    "reason": item.reason,
                    "occurred_at": item.occurred_at.isoformat(),
                    "window_start": item.window_start.isoformat(),
                    "window_end": item.window_end.isoformat(),
                    "competing_device_ids": list(item.competing_device_ids),
                    "power_window_contaminated": item.power_window_contaminated,
                }
                for item in verdicts
            ],
        }

    def _coverage(
        self,
        occurred_at: datetime,
        histories: Mapping[str, Sequence[tuple[datetime, float]]],
        device_id: str = "",
    ) -> SampleCoverage | None:
        """Usable readings before and after the event, across every channel.

        ``None`` means "not measured" -- the same value the module treats as
        insufficient.  Counting samples that do not exist would be worse than
        admitting the window was never observed.

        The window is the *resolved* one for ``device_id``: counting coverage on
        the global window while the verdict cites a per-device window would
        measure a different interval than the one the verdict is about.
        """
        if not histories:
            return None
        window, _source = self.config.isolation.resolve(
            str(device_id), self._device_classes.get(str(device_id), ())
        )
        start = occurred_at + timedelta(seconds=window.analysis_offset_s)
        isolation_end = start + timedelta(seconds=window.isolation_window_s)
        power_end = start + timedelta(seconds=window.analysis_window_s)
        baseline = 0
        event = 0
        for points in histories.values():
            for stamp, _watts in points:
                if start <= stamp < isolation_end:
                    baseline += 1
                elif isolation_end <= stamp < power_end:
                    event += 1
        return SampleCoverage(baseline_samples=baseline, event_samples=event)

    # -- stage 2: association ---------------------------------------------- #
    def _associate(
        self,
        isolations: Sequence[EventIsolation],
        histories: Mapping[str, Sequence[tuple[datetime, float]]],
        by_device: Mapping[str, InferenceResult],
        registry: FactorRegistry,
        graph: Any,
        at: datetime,
        report: AuditReport,
    ) -> list[TopologyRelation]:
        if not isolations:
            return []
        finder = AssociationFinder(self.config.association)
        polarity = {
            str(channel.ct_id): _enum_text(channel.polarity)
            for channel in graph.channels.values()
        }
        observations: list[Observation] = []
        for verdict in sorted(
            isolations, key=lambda item: (item.occurred_at, item.device_id, item.event_id)
        ):
            key = f"association:{verdict.event_id}"
            try:
                before, after = self._windows(verdict, histories)
                result = by_device.get(str(verdict.device_id))
                device_id = str(verdict.device_id)
                if result is not None:
                    settled = float(result.power_w)
                    delta = self._device_delta(device_id, settled, verdict.occurred_at)
                else:
                    # No live result for this device (a reanalysis pass started
                    # from stored history after a restart).  Its own recorded
                    # power after the window is the honest "after" value; without
                    # it the step is reported unknown rather than assumed zero.
                    settled_at = self._power_at(
                        device_id,
                        verdict.window_end
                        + timedelta(seconds=self.config.isolation.power_window_s),
                    )
                    delta = (
                        None
                        if settled_at is None
                        else self._device_delta(device_id, settled_at, verdict.occurred_at)
                    )
                found = finder.observe(
                    verdict,
                    before,
                    after,
                    [],
                    device_delta_w=delta,
                    polarity=polarity,
                )
            except Exception as exc:  # noqa: BLE001 - per-event isolation
                report.errors[key] = f"{type(exc).__name__}: {exc}"
                continue
            self._remember_observations(found)
            observations.extend(found)
        # Association is a claim about *repeated* response: ``min_events``
        # independent events, minimum two, because one switching cannot separate
        # a real association from a coincidence.  A pass runs every few seconds
        # and sees at most one event per device, so analysing only the current
        # window would never reach two and no relation would ever be emitted.
        # The accumulated evidence is re-analysed instead, deduped by
        # (event, CT) so a re-analysis of the same event cannot count twice.
        accumulated = self._accumulated_observations()
        if accumulated:
            observations = accumulated
        report.counts["observations"] = len(observations)
        if not observations:
            return []
        try:
            relations = finder.analyze(observations, registry=registry, now=at)
        except Exception as exc:  # noqa: BLE001 - the pass continues
            report.errors["association"] = f"{type(exc).__name__}: {exc}"
            return []
        report.stages["association"] = {
            "withheld": {
                str(device): dict(counts)
                for device, counts in self._withheld_counts(
                    finder, observations, registry, at
                ).items()
            }
        }
        report.counts["associations"] = sum(
            1
            for item in relations
            if RelationKind(_enum_text(item.kind)) is RelationKind.DEVICE_CT_ASSOCIATION
        )
        if relations and self.config.write_association:
            report.written["associations"] = self._write(
                persist_relations, relations, report, "associations"
            )
        return relations

    def _withheld_counts(
        self,
        finder: AssociationFinder,
        observations: Sequence[Observation],
        registry: FactorRegistry,
        at: datetime,
    ) -> dict[str, dict[str, int]]:
        """Why the finder emitted nothing for a device, from its own report.

        ``withheld`` is a *method*, not an attribute: the finder recomputes the
        whole decision so a caller cannot read a half-updated counter.  Reported
        here, never re-derived locally -- a second implementation of the reasons
        would be able to disagree with the one that made the decision.
        """
        try:
            return {
                str(device): dict(counts)
                for device, counts in finder.withheld(
                    observations, registry=registry, now=at
                ).items()
            }
        except Exception:  # noqa: BLE001 - the explanation is not the decision
            return {}

    def _remember_observations(self, observations: Sequence[Observation]) -> None:
        """Keep association evidence across passes, deduped by (event, CT).

        A verdict is about an event, so re-analysing the same event must be the
        same observation.  Keying on ``(event_id, ct_id)`` makes the buffer
        idempotent: a reanalysis pass re-observing history cannot inflate the
        event count that ``min_events`` is judged against.
        """
        for row in observations:
            key = (row.device_id, row.event_id, row.ct_id)
            if key not in self._observation_index:
                self._observation_order.append(key)
                self._observation_index[key] = row
        while len(self._observation_order) > DEFAULT_OBSERVATION_HISTORY:
            oldest = self._observation_order.popleft()
            self._observation_index.pop(oldest, None)

    def _accumulated_observations(self) -> list[Observation]:
        """Every remembered observation, in arrival order (deterministic)."""
        return [
            self._observation_index[key]
            for key in self._observation_order
            if key in self._observation_index
        ]

    def _seed_power_history(
        self, topology: Topology, start: datetime, end: datetime
    ) -> None:
        """Seed per-device power baselines from stored inference results.

        A stored-history pass re-derives events that happened before it started,
        so the in-memory baseline (which only holds this process's cycles) has no
        entry for them and every association would be judged against
        "unknown step".  The stored results *are* the evidence of what each
        device was drawing then, so they are read rather than invented.
        """
        database = self.database
        if database is None:
            return
        for device in topology.devices:
            try:
                rows = database.inference_history(
                    str(device.device_id), since=start, limit=2000
                )
            except Exception:  # noqa: BLE001 - a read failure is not fatal
                continue
            for row in rows:
                stamp = _as_datetime(row.get("timestamp"))
                power = _float_or_none(row.get("power_w"))
                if stamp is None or power is None or stamp > end:
                    continue
                if _enum_text(row.get("source")) == Source.UNKNOWN.value:
                    continue
                history = self._power_history.setdefault(
                    str(device.device_id), deque(maxlen=DEFAULT_POWER_HISTORY)
                )
                if history and history[-1][0] == stamp:
                    history[-1] = (stamp, power)
                    continue
                history.append((stamp, power))
        for history in self._power_history.values():
            ordered = sorted(history, key=lambda item: item[0])
            history.clear()
            history.extend(ordered)

    def _device_delta(self, device_id: str, after: float, occurred_at: datetime) -> float | None:
        """The device's own power step across the event, in W.

        The baseline is the device's inferred power *at the event*, read from the
        per-device history this pass records -- not "the value from the previous
        cycle".  The pass runs every few seconds and analyses an event only once
        its window has elapsed, so by then the previous cycle's value is already
        the post-event one and the step would read as ~0.  That is why
        association reported ``insufficient_magnitude`` for every real
        transition on a live install.

        ``None`` (no history spanning the event) is returned as unknown so the
        finder can derive its own step from device samples instead of being told
        a step of zero.

        The baseline must be *strictly* before ``occurred_at``.  The pass
        records this cycle's inferred power under the cycle's own timestamp,
        which for an event that arrived this cycle is the event instant itself;
        accepting ``<=`` therefore compared the post-event value with itself and
        reported a step of 0 W for every real transition.
        """
        points = self._power_history.get(device_id)
        if not points:
            return None
        before: float | None = None
        for stamp, watts in points:
            if stamp < occurred_at:
                before = watts
            else:
                break
        if before is None:
            return None
        return float(after) - float(before)

    def _power_at(self, device_id: str, at: datetime) -> float | None:
        """The last inferred power recorded at or before ``at``, else ``None``.

        Used by a stored-history pass to read what a device was drawing at a
        moment this process never observed live.  ``None`` means "not known",
        which the finder treats as an unknown step rather than a zero one.
        """
        points = self._power_history.get(device_id)
        if not points:
            return None
        found: float | None = None
        for stamp, watts in points:
            if stamp <= at:
                found = watts
            else:
                break
        return None if found is None else float(found)

    def _record_power(self, device_id: str, at: datetime, power_w: float) -> None:
        """Append one inferred power point per device, bounded per device."""
        if not math.isfinite(float(power_w)):
            return
        history = self._power_history.setdefault(device_id, deque(maxlen=DEFAULT_POWER_HISTORY))
        if history and history[-1][0] == at:
            history[-1] = (at, float(power_w))
            return
        history.append((at, float(power_w)))

    def _windows(
        self, verdict: EventIsolation, histories: Mapping[str, Sequence[tuple[datetime, float]]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """CT sample rows strictly before and after the event.

        The baseline is what the channel was drawing *before* ``window_start``,
        not the isolation window itself.  The isolation window is the interval
        in which a *competing* load would contaminate the event; it is not a
        baseline.  Reading it as one puts both halves of the comparison after
        the transition, so every CT delta collapsed to a few watts of noise and
        no association could ever reach ``min_magnitude_w``.

        Both halves are ``power_window_s`` wide so the two medians rest on the
        same amount of evidence.  A step from an earlier event inside the
        baseline is tolerated by the median, which is why the window can be
        this wide without inventing a level.

        The width is deliberately the *global* power window, not the resolved
        per-device one: this is the window the CT delta is *measured* in, not
        the window a competing load would contaminate, and widening it for some
        devices would change every association delta they contribute -- an
        algorithm change, not a wiring fix.  Per-device isolation timings
        therefore govern the verdict, the coverage count and the hold horizon.
        """
        boundary = verdict.window_start
        wide = timedelta(seconds=self.config.isolation.power_window_s)
        start = boundary - wide
        power_end = boundary + wide
        before: list[dict[str, Any]] = []
        after: list[dict[str, Any]] = []
        for ct_id in sorted(histories):
            for stamp, watts in histories[ct_id]:
                row = {
                    "ct_id": ct_id,
                    "state_timestamp": stamp.isoformat(),
                    "value_w": float(watts),
                }
                if start <= stamp < boundary:
                    before.append(row)
                elif boundary <= stamp < power_end:
                    after.append(row)
        return before, after

    # -- stage 3: hierarchy ------------------------------------------------ #
    def _hierarchy(
        self,
        relations: Sequence[TopologyRelation],
        graph: Any,
        histories: Mapping[str, Sequence[tuple[datetime, float]]],
        registry: FactorRegistry,
        at: datetime,
        report: AuditReport,
    ) -> list[TopologyRelation]:
        """Overlap first, then (only with series) parent/child.

        ``overlaps`` needs no series and is what keeps "one device on two CTs" a
        measurement overlap instead of a parent link -- the Hot Tub bar.  It
        must therefore run even when ``infer`` has nothing to work with.
        """
        declared = self._declared_parents(graph)
        finder = HierarchyFinder(self.config.hierarchy, declared=declared)
        out: list[TopologyRelation] = []
        try:
            overlaps = finder.overlaps(relations)
        except Exception as exc:  # noqa: BLE001
            report.errors["hierarchy_overlaps"] = f"{type(exc).__name__}: {exc}"
            overlaps = []
        report.counts["overlaps"] = len(overlaps)
        out.extend(overlaps)

        series = aligned_series(histories)
        inferred: list[TopologyRelation] = []
        if series:
            try:
                inferred = finder.infer(
                    relations,
                    [],
                    series=series,
                    registry=registry,
                    declared=declared,
                    now=at,
                    channels={
                        str(cid): {"ct_id": str(cid)}
                        for cid in graph.channels
                    },
                )
            except Exception as exc:  # noqa: BLE001
                report.errors["hierarchy_infer"] = f"{type(exc).__name__}: {exc}"
        report.stages["hierarchy"] = {
            "refused": dict(finder.report.get("refused") or {}),
            "reason": finder.report.get("reason"),
            "explanation": finder.report.get("explanation"),
            "declared_wins": list(finder.report.get("declared_wins") or ()),
            "series_channels": len(series),
        }
        parent_child = [
            item
            for item in inferred
            if RelationKind(_enum_text(item.kind)) is RelationKind.ELECTRICAL_PARENT_CHILD
        ]
        report.counts["parent_child"] = len(parent_child)
        out.extend(inferred)

        if out and self.config.write_hierarchy:
            report.written["hierarchy"] = self._write(persist_relations, out, report, "hierarchy")
        return out

    def _declared_parents(self, graph: Any) -> list[TopologyRelation]:
        out: list[TopologyRelation] = []
        for channel in graph.channels.values():
            parent = getattr(channel, "parent_ct_id", None)
            if not parent:
                continue
            out.append(
                TopologyRelation(
                    relation_id=f"declared_parent:{channel.ct_id}->{parent}",
                    kind=RelationKind.ELECTRICAL_PARENT_CHILD,
                    subject_id=str(channel.ct_id),
                    object_id=str(parent),
                    status=TopologyStatus.CONFIRMED,
                    requires_confirmation=False,
                    declared_by="user",
                    evidence={"source": "declared_parent_ct_id"},
                )
            )
        return out

    # -- stage 4: circuits + residual -------------------------------------- #
    def _circuits(
        self,
        relations: Sequence[TopologyRelation],
        registry: FactorRegistry,
        graph: Any,
        by_device: Mapping[str, InferenceResult],
        readings: Mapping[str, float],
        at: datetime,
        report: AuditReport,
    ) -> list[Circuit]:
        classification = [
            item
            for item in relations
            if RelationKind(_enum_text(item.kind)) is RelationKind.DEVICE_CT_ASSOCIATION
        ]
        if not classification:
            return []
        classifier = CircuitClassifier(self.config.circuits)
        try:
            candidates = classifier.candidates(
                classification, registry=registry, now=at, topology=graph
            )
        except Exception as exc:  # noqa: BLE001
            report.errors["circuit_candidates"] = f"{type(exc).__name__}: {exc}"
            return []
        residuals = self._residuals(
            candidates, graph, registry, by_device, readings, at, report
        )
        out: list[Circuit] = []
        for circuit in candidates:
            key = f"circuit:{circuit.circuit_id}"
            try:
                out.append(
                    classifier.classify(
                        circuit,
                        classification,
                        residual=residuals.get(circuit.circuit_id),
                        now=at,
                    )
                )
                report.counts["circuits_classified"] += 1
            except Exception as exc:  # noqa: BLE001 - one circuit never stops the pass
                report.errors[key] = f"{type(exc).__name__}: {exc}"
                out.append(circuit)
        report.counts["circuits"] = len(out)
        if out and self.config.write_circuits:
            report.written["circuits"] = self._write(
                lambda items, database: persist_circuits(items, database, now=at),
                out,
                report,
                "circuits",
            )
        return out

    def _residuals(
        self,
        circuits: Sequence[Circuit],
        graph: Any,
        registry: FactorRegistry,
        by_device: Mapping[str, InferenceResult],
        readings: Mapping[str, float],
        at: datetime,
        report: AuditReport,
    ) -> dict[str, ResidualPower]:
        """One residual per *single-CT* circuit scope.

        A multi-CT circuit is deliberately not summed.  Two clamps may be the two
        legs of one 240 V appliance (in which case the total is their sum) or two
        monitors on one wire (in which case the sum is a double count), and
        nothing in the evidence distinguishes them.  The scope is recorded as
        refused with that reason instead of producing a number that would be
        wrong half the time.
        """
        if not circuits:
            return {}
        analyzer = ResidualAnalyzer(graph)
        out: dict[str, ResidualPower] = {}
        for circuit in circuits:
            ct_ids = [str(ct_id) for ct_id in circuit.ct_ids]
            if len(ct_ids) != 1:
                report.refused[f"residual:{circuit.circuit_id}"] = REFUSED_MULTI_CT_SCOPE
                continue
            ct_id = ct_ids[0]
            reading = readings.get(ct_id)
            if reading is None:
                report.refused[f"residual:{circuit.circuit_id}"] = REFUSED_NO_READING
                continue
            basis, _multiplier, _confidence = registry.basis_for(ct_id, at)
            if MeasurementBasis(_enum_text(basis)) is MeasurementBasis.UNKNOWN:
                report.refused[f"residual:{circuit.circuit_id}"] = REFUSED_UNKNOWN_BASIS
                continue
            contributions = {
                str(device_id): float(result.power_w)
                for device_id, result in by_device.items()
                if str(device_id) in circuit.device_ids
                and _enum_text(result.source) != Source.UNKNOWN.value
            }
            scope = ResidualScope(
                scope_id=f"scope:{circuit.circuit_id}",
                ct_id=ct_id,
                basis=basis,
                device_contributions=contributions,
            )
            try:
                attempt = analyzer.try_compute(scope, float(reading), now=at)
            except Exception as exc:  # noqa: BLE001 - one scope never stops the pass
                report.errors[f"residual:{circuit.circuit_id}"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            if attempt.residual is None:
                report.refused[f"residual:{circuit.circuit_id}"] = str(
                    attempt.reason_code or "not_computed"
                )
                continue
            out[circuit.circuit_id] = attempt.residual
            report.refused.pop(f"residual:{circuit.circuit_id}", None)
        values = [out[key] for key in sorted(out)]
        report.counts["residuals"] = len(values)
        # Recorded on the report, not only counted: the API serves the last
        # pass's residuals and a pass that counted them without keeping them
        # would report "160 residuals" and hand back an empty list.
        report.residuals = list(values)
        if values and self.config.write_residual:
            report.written["residuals"] = self._write(
                lambda items, database: persist_residual(items, database, timestamp=at),
                values,
                report,
                "residuals",
            )
        return out

    # -- stage 5: records -------------------------------------------------- #
    def _records(
        self,
        by_device: Mapping[str, InferenceResult],
        isolations: Sequence[EventIsolation],
        relations: Sequence[TopologyRelation],
        circuits: Sequence[Circuit],
        topology: Topology,
        at: datetime,
        report: AuditReport,
    ) -> list[InferenceRecord]:
        if not self.config.write_records:
            return []
        by_isolation: dict[str, list[EventIsolation]] = {}
        for verdict in isolations:
            by_isolation.setdefault(str(verdict.device_id), []).append(verdict)
        out: list[InferenceRecord] = []
        for device_id in sorted(by_device):
            key = f"record:{device_id}"
            previous = self._previous(device_id)
            try:
                if not self._changed(device_id, by_device[device_id], previous, at):
                    # Nothing about this device moved: writing a row per cycle
                    # would append the same fact forever and hide the ones that
                    # did change.  This is a state *change* trail, not a poll log.
                    continue
                record = self._record(
                    device_id,
                    by_device[device_id],
                    by_isolation.get(device_id, ()),
                    relations,
                    circuits,
                    topology,
                    at,
                    previous=previous,
                )
            except Exception as exc:  # noqa: BLE001 - one device never stops the pass
                report.errors[key] = f"{type(exc).__name__}: {exc}"
                continue
            if previous is not None and record.record_id == previous.get("record_id"):
                # Same instant, same revision, same value: a replay of a record
                # that is already stored.  Rewriting it would be an overwrite.
                continue
            out.append(record)
            self._last_revision[device_id] = int(record.revision)
            self._last_record[device_id] = record.record_id
            self._last_confidence[device_id] = record.confidence
        # ``_last_power`` tracks the last *inferred* power per device, which is
        # the baseline the next event's delta is measured against.  It must be
        # refreshed for every device on every pass, not only where a record was
        # written: the trail records changes, so a steady device would otherwise
        # keep a stale baseline and report a zero step for a real transition.
        for device_id, result in by_device.items():
            with contextlib.suppress(TypeError, ValueError):
                self._last_power[str(device_id)] = float(result.power_w)
        report.counts["records"] = len(out)
        if out:
            report.written["records"] = self._write(
                persist_records, out, report, "records"
            )
        return out

    def _changed(
        self,
        device_id: str,
        result: InferenceResult,
        previous: Mapping[str, Any] | None,
        at: datetime,
    ) -> bool:
        """Whether this inference differs enough from the stored one to record.

        First record always writes.  After that: a moved power value, a changed
        source/basis, or an elapsed heartbeat.  The heartbeat exists so a long
        steady state still proves the analysis pass was alive, without turning
        the trail into one row per cycle.
        """
        if previous is None:
            return True
        previous_power = _float_or_none(previous.get("power_w"))
        if previous_power is None:
            return True
        if abs(float(result.power_w) - previous_power) > RECORD_POWER_TOLERANCE_W:
            return True
        if _enum_text(previous.get("source")) != _enum_text(result.source):
            return True
        if _enum_text(previous.get("measurement_basis")) != _enum_text(
            result.measurement_basis
        ):
            return True
        previous_confidence = _coerce_confidence(previous.get("confidence"))
        if previous_confidence is not None:
            for name, value in ConfidenceVector(
                power=_bounded(float(result.confidence))
            ).dimensions().items():
                before = getattr(previous_confidence, name, None)
                if before is not None and abs(float(value) - float(before)) > (
                    RECORD_CONFIDENCE_TOLERANCE
                ):
                    return True
        stamp = _as_datetime(previous.get("timestamp"))
        if stamp is None:
            return True
        return (at - stamp).total_seconds() >= RECORD_HEARTBEAT_S

    def _record(
        self,
        device_id: str,
        result: InferenceResult,
        isolations: Sequence[EventIsolation],
        relations: Sequence[TopologyRelation],
        circuits: Sequence[Circuit],
        topology: Topology,
        at: datetime,
        *,
        previous: Mapping[str, Any] | None = None,
    ) -> InferenceRecord:
        previous_row = previous if previous is not None else self._previous(device_id)
        if previous_row is not None:
            revision = int(previous_row.get("revision") or 0) + 1
            previous_id = str(previous_row.get("record_id") or "") or None
            previous_power = _float_or_none(previous_row.get("power_w"))
            previous_confidence = _coerce_confidence(previous_row.get("confidence"))
        else:
            revision = int(self._last_revision.get(device_id, 0)) + 1
            previous_id = self._last_record.get(device_id)
            previous_power = self._last_power.get(device_id)
            previous_confidence = self._last_confidence.get(device_id)

        associations = [
            item
            for item in relations
            if RelationKind(_enum_text(item.kind)) is RelationKind.DEVICE_CT_ASSOCIATION
            and str(item.subject_id) == device_id
        ]
        circuits_here = [
            circuit for circuit in circuits if device_id in circuit.device_ids
        ]
        # Every ref must name a row that exists after a restart.  Isolation
        # verdicts are persisted (migration 10), relations and circuits are
        # persisted by their own stages, and when a record has none of those it
        # falls back to its own persisted ``record_id`` -- not to a synthetic
        # ``result:`` string that no lookup could ever resolve.
        evidence_refs = self._resolvable_refs(
            [
                *(f"event:{item.event_id}" for item in isolations),
                *(f"relation:{item.relation_id}" for item in associations),
                *(f"circuit:{item.circuit_id}" for item in circuits_here),
            ]
        )
        source = result.source
        if _enum_text(source) == Source.UNKNOWN.value:
            # An unknown source cites nothing because it asserts nothing.  This
            # is the contract's own rule, not a shortcut here.
            evidence_refs = []
        elif not evidence_refs:
            # The record itself is persisted by this pass, so it is the one
            # reference that always resolves.  A self-reference is weaker
            # evidence than a raw observation, so it is recorded as such.
            evidence_refs = [f"record:{self._record_id(device_id, at, revision)}"]

        confidence = ConfidenceVector(
            power=_bounded(float(result.confidence)),
            ct_association=_max_confidence(associations),
            circuit_classification=_circuit_confidence(circuits_here),
            method=f"audit:{self.config.algorithm_version}",
        )
        return InferenceRecord(
            record_id=self._record_id(device_id, at, revision),
            device_id=device_id,
            timestamp=at,
            algorithm_version=self.config.algorithm_version,
            topology_version=int(getattr(topology, "version", 1) or 1),
            revision=revision,
            previous_record_id=None if previous_id == "" else previous_id,
            previous_power_w=previous_power,
            power_w=float(result.power_w),
            source=source,
            measurement_basis=result.measurement_basis,
            confidence=confidence,
            previous_confidence=previous_confidence,
            confidence_delta=_confidence_delta(previous_confidence, confidence),
            reason=self._reason(result, isolations, associations),
            explanation=[
                f"source={_enum_text(source)}",
                f"tier={_enum_text(result.confidence_tier)}",
                *self._explain(isolations, associations, circuits_here),
            ],
            evidence_refs=evidence_refs,
            evidence={
                "window_s": float(self.config.isolation_window_s),
                "isolations": [
                    {
                        "event_id": item.event_id,
                        "status": _enum_text(item.status),
                        "reason": item.reason,
                    }
                    for item in isolations
                ],
                "association_ids": sorted(item.relation_id for item in associations),
                "circuit_ids": sorted(item.circuit_id for item in circuits_here),
                "audit": self.config.algorithm_version,
            },
        )

    def _resolvable_refs(self, refs: Iterable[str]) -> list[str]:
        """Keep only refs whose row this pass actually persisted.

        A reference is a claim that a reader can follow it.  When a family's
        write is disabled (or storage is absent) the corresponding rows do not
        exist, so citing them would be a dangling claim -- worse than citing
        fewer observations.  The test suite asserts every emitted ref resolves.
        """
        database = self.database
        if database is None:
            return []
        resolve = getattr(database, "resolve_evidence_ref", None)
        if not callable(resolve):
            return list(refs)
        out: list[str] = []
        for ref in refs:
            try:
                if resolve(ref) and ref not in out:
                    out.append(ref)
            except Exception:  # noqa: BLE001 - a broken lookup must not stop a record
                continue
        return out

    def _record_id(self, device_id: str, at: datetime, revision: int) -> str:
        """The deterministic id this record will be stored under."""
        return record_id_for(device_id, self.config.algorithm_version, at, revision)

    def _reason(
        self,
        result: InferenceResult,
        isolations: Sequence[EventIsolation],
        associations: Sequence[TopologyRelation],
    ) -> str:
        """A short machine code, derived only from evidence.

        Deterministic: the same inputs pick the same branch, which is what makes
        a replay's ``reason`` identical (``J9``).
        """
        statuses = {_enum_text(item.status) for item in isolations}
        if IsolationStatus.ISOLATED.value in statuses:
            return "isolated_event"
        if IsolationStatus.CONTAMINATED.value in statuses:
            return "contaminated_event"
        if associations:
            return "associated_measurement"
        source = _enum_text(result.source)
        if source == Source.DIRECT_MEASURED.value:
            return "direct_measurement"
        if source == Source.MODEL_ESTIMATED.value:
            return "model_estimate"
        if source == Source.CONFIGURED_PRIOR.value:
            return "configured_prior"
        return "no_attribution"

    @staticmethod
    def _explain(
        isolations: Sequence[EventIsolation],
        associations: Sequence[TopologyRelation],
        circuits: Sequence[Circuit],
    ) -> list[str]:
        lines: list[str] = []
        for verdict in isolations[:5]:
            lines.append(
                f"isolation:{verdict.event_id}={_enum_text(verdict.status)} ({verdict.reason})"
            )
        for relation in associations[:5]:
            lines.append(
                f"association:{relation.object_id} relation={relation.relation_id}"
            )
        for circuit in circuits[:5]:
            lines.append(
                f"circuit:{circuit.circuit_id}={_enum_text(circuit.classification)}"
            )
        return lines

    # -- writing ------------------------------------------------------------ #
    def _write(self, writer: Any, items: Sequence[Any], report: AuditReport, name: str) -> int:
        database = self.database
        if database is None or not items:
            return 0
        try:
            return int(writer(list(items), database) or 0)
        except Exception as exc:  # noqa: BLE001 - a write failure is reported, not fatal
            report.errors[f"write:{name}"] = f"{type(exc).__name__}: {exc}"
            return 0

    # -- readings ---------------------------------------------------------- #
    def observe_readings(self, window: Any) -> dict[str, float]:
        """This window's per-channel readings, for the residual arithmetic.

        ``readings`` is empty on a reanalysis window (stored history carries no
        live snapshot), which is why a residual scope without a current reading
        is reported as refused rather than computed from a stale one.
        """
        readings: dict[str, float] = {}
        raw = getattr(window, "readings", None)
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                signed = getattr(value, "signed_w", None)
                watts = signed() if callable(signed) else getattr(value, "value_w", value)
                if watts is None:
                    continue
                try:
                    readings[str(key)] = float(watts)
                except (TypeError, ValueError):
                    continue
        else:
            channels = getattr(window, "channels", None)
            if isinstance(channels, Mapping):
                for key, value in channels.items():
                    watts = getattr(value, "value_w", None)
                    if watts is None:
                        continue
                    try:
                        readings[str(key)] = float(watts)
                    except (TypeError, ValueError):
                        continue
        self._readings = readings
        return readings


@dataclass(frozen=True, slots=True)
class _StoredWindow:
    """The minimum a pass needs from a window it did not receive live."""

    at: datetime
    events: Sequence[Any] = ()
    histories: Mapping[str, Sequence[tuple[datetime, float]]] = field(
        default_factory=dict
    )

    @property
    def state_transitions(self) -> tuple[Any, ...]:
        return tuple(self.events)

    @property
    def readings(self) -> dict[str, Any]:
        return {}


# --------------------------------------------------------------------------- #
# Small coercions
# --------------------------------------------------------------------------- #
def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bounded(value: float) -> float:
    if not math.isfinite(float(value)):
        return 0.0
    return min(1.0, max(0.0, float(value)))


def _max_confidence(relations: Sequence[TopologyRelation]) -> float | None:
    values = [
        float(item.confidence)
        for item in relations
        if item.confidence is not None and math.isfinite(float(item.confidence))
    ]
    return max(values) if values else None


def _circuit_confidence(circuits: Sequence[Circuit]) -> float | None:
    values: list[float] = []
    for circuit in circuits:
        raw = (circuit.metadata or {}).get("classification_confidence")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            values.append(float(raw))
    return max(values) if values else None


def _coerce_confidence(raw: Any) -> ConfidenceVector | None:
    if raw is None:
        return None
    if isinstance(raw, ConfidenceVector):
        return raw
    if isinstance(raw, Mapping):
        try:
            return ConfidenceVector.model_validate(dict(raw))
        except Exception:  # noqa: BLE001 - a malformed stored vector is not fatal
            return None
    return None


__all__ = [
    "AuditConfig",
    "AuditPass",
    "AuditReport",
    "COUNT_KEYS",
    "DEFAULT_ALGORITHM_VERSION",
    "REFUSED_MULTI_CT_SCOPE",
    "REFUSED_NO_READING",
    "REFUSED_UNKNOWN_BASIS",
    "aligned_series",
    "history_from_rows",
    "persist_isolations",
    "persist_records",
    "record_id_for",
]
