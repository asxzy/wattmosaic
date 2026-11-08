"""Measurement matrix ``y = A x`` over the CT topology.

Definitions
-----------
``y``  measured CT power (watts, polarity corrected, sign preserved)
``x``  unknown per-device power (watts, total power of the logical device)
``A``  (``m`` rows x ``n`` devices) electrical incidence matrix
``e``  noise, unmodelled loads, measurement error

Row construction rules (see ``docs/electrical_model.md`` for the worked
examples):

1. A **dedicated CT** row contains ``1`` for its device (``1`` on a ``BOTH``
   row, phase split for a single-line row of a double-pole pair).
2. A **branch CT** row contains ``1`` only for devices whose *most specific*
   attribution CT is that branch CT.  Devices living under a deeper child CT
   are **not** repeated in the parent row; the child CT value is subtracted
   explicitly at solve time (:meth:`MeasurementMatrix.prepare`).  If the child
   value is unavailable the parent row is *excluded* with a reason.
3. A **main CT** row contains ``1`` for every device below it (containment),
   because a main CT genuinely measures the total of its subtree.  Main rows
   are never used to build a total by addition; each device appears once per
   row and the total is computed from the per-device vector only.
4. Devices that appear in no row are returned in ``unobserved_devices``; they
   must not be silently reported as zero.

The same physical device therefore never receives two *independent* power
attributions: it has exactly one set of attribution rows.  Redundant main rows
are additional constraints on the same ``x``, not extra power.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from wattmosaic_engine.contracts import CTType, Phase
from wattmosaic_engine.electrical.topology import TopologyGraph

DEFAULT_NOISE_FLOOR_W = 1.0


def row_sigma(channel, *, noise_floor_w: float = DEFAULT_NOISE_FLOOR_W) -> float:
    """Documented per-row measurement standard deviation (watts).

    ``sigma = max(noise_floor, resolution / 2) / reliability``

    The floor prevents a channel that reports a tiny resolution from receiving
    an infinite weight.  Reliability in ``(0, 1]`` divides the sigma: a channel
    with reliability 0.5 is treated as twice as noisy.
    """
    reliability = float(getattr(channel, "reliability", 1.0) or 1.0)
    reliability = min(max(reliability, 0.05), 1.0)
    res = getattr(channel, "resolution_w", None)
    sigma = max(noise_floor_w, float(res) / 2.0) if res else noise_floor_w
    return sigma / reliability


@dataclass(frozen=True, slots=True)
class PreparedSystem:
    """Concrete solvable system for one observation window."""

    matrix: MeasurementMatrix
    y: np.ndarray
    weights: np.ndarray
    mask: np.ndarray
    excluded_rows: dict[str, str] = field(default_factory=dict)

    @property
    def active_row_channel_ids(self) -> tuple[str, ...]:
        return tuple(
            cid for cid, ok in zip(self.matrix.row_channel_ids, self.mask, strict=False) if ok
        )

    def reduced(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        idx = np.flatnonzero(self.mask)
        A = self.matrix.A[idx] * self.weights[idx, None]
        b = self.y[idx] * self.weights[idx]
        return A, b, idx


@dataclass(slots=True)
class MeasurementMatrix:
    """``A`` plus row/column metadata and uncertainty weights."""

    A: np.ndarray
    row_channel_ids: list[str]
    col_device_ids: list[str]
    row_weights: np.ndarray
    row_sigma: np.ndarray
    row_phases: list[Phase]
    row_kind: dict[str, str]
    row_notes: dict[str, list[str]] = field(default_factory=dict)
    nested_children: dict[str, tuple[str, ...]] = field(default_factory=dict)
    channel_polarity: dict[str, str] = field(default_factory=dict)
    #: Per-channel circuit multiplier (default 1.0).  Applied once, inside
    #: :meth:`_reading_w`, so every row of the fit sees a consistently scaled
    #: measurement and the multiplier is never applied twice.
    channel_multiplier: dict[str, float] = field(default_factory=dict)
    unobserved_devices: list[str] = field(default_factory=list)
    topology_version: int = 1
    phase_split_l1: float = 0.5

    # -- introspection --------------------------------------------------------
    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.row_channel_ids), len(self.col_device_ids))

    def row_index(self, channel_id: str) -> int | None:
        try:
            return self.row_channel_ids.index(channel_id)
        except ValueError:
            return None

    def col_index(self, device_id: str) -> int | None:
        try:
            return self.col_device_ids.index(device_id)
        except ValueError:
            return None

    def coefficient(self, channel_id: str, device_id: str) -> float:
        r = self.row_index(channel_id)
        c = self.col_index(device_id)
        if r is None or c is None:
            return 0.0
        return float(self.A[r, c])

    def supported_rows(self, device_id: str) -> list[str]:
        c = self.col_index(device_id)
        if c is None:
            return []
        return [
            cid
            for i, cid in enumerate(self.row_channel_ids)
            if abs(self.A[i, c]) > 0.0
        ]

    def column(self, device_id: str) -> np.ndarray:
        c = self.col_index(device_id)
        if c is None:
            return np.zeros(self.shape[0])
        return self.A[:, c]

    # -- targets --------------------------------------------------------------
    def _reading_w(self, readings: Mapping[str, object], channel_id: str) -> float | None:
        value = self._raw_reading_w(readings, channel_id)
        if value is None:
            return None
        m = self._channel_multiplier(channel_id)
        return value if m == 1.0 else value * m

    def _raw_reading_w(self, readings: Mapping[str, object], channel_id: str) -> float | None:
        raw = readings.get(channel_id)
        if raw is None:
            return None
        if hasattr(raw, "signed_w"):
            return raw.signed_w()  # ChannelReading
        if hasattr(raw, "effective_power_w"):
            return float(raw.effective_power_w())  # Measurement
        if isinstance(raw, Mapping) and ("value_w" in raw or "value" in raw):
            val = raw.get("value_w", raw.get("value"))
            if val is None:
                return None
            pol = raw.get("polarization", raw.get("polarity", "normal"))
            return -float(val) if str(pol).endswith("inverted") else float(val)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = float(raw)
            if not math.isfinite(value):
                return None
            channel = self._channel_polarity(channel_id)
            return -value if channel == "inverted" else value
        return None

    def _channel_polarity(self, channel_id: str) -> str:
        return self.channel_polarity.get(channel_id, "normal")

    def _channel_multiplier(self, channel_id: str) -> float:
        return float(self.channel_multiplier.get(channel_id, 1.0) or 1.0)

    def prepare(
        self,
        readings: Mapping[str, object],
        *,
        extra_target_map: Mapping[str, float] | None = None,
    ) -> PreparedSystem:
        """Build ``y`` for the current readings.

        Branch rows with nested child CTs get ``y_parent - sum(child)``.  A
        missing/unusable child value excludes the parent row (recorded in
        ``excluded_rows``) instead of double counting the child.
        """
        m = self.shape[0]
        y = np.zeros(m)
        mask = np.ones(m, dtype=bool)
        excluded: dict[str, str] = {}
        extra_target_map = extra_target_map or {}
        for i, cid in enumerate(self.row_channel_ids):
            value = extra_target_map.get(cid)
            if value is None:
                value = self._reading_w(readings, cid)
            if value is None:
                mask[i] = False
                excluded[cid] = "missing_measurement"
                continue
            children = self.nested_children.get(cid, ())
            subtotal = 0.0
            child_missing: list[str] = []
            for child in children:
                child_value = extra_target_map.get(child)
                if child_value is None:
                    child_value = self._reading_w(readings, child)
                if child_value is None:
                    child_missing.append(child)
                else:
                    subtotal += child_value
            if child_missing:
                mask[i] = False
                excluded[cid] = "nested_child_missing:" + ",".join(sorted(child_missing))
                continue
            y[i] = value - subtotal
            self.row_notes.setdefault(cid, [])
            if children:
                note = "subtracts_nested_cts:" + ",".join(children)
                if note not in self.row_notes[cid]:
                    self.row_notes[cid].append(note)
        return PreparedSystem(
            matrix=self,
            y=y,
            weights=np.array(self.row_weights, dtype=float),
            mask=mask,
            excluded_rows=excluded,
        )

    def predict(self, x: Sequence[float]) -> np.ndarray:
        return np.asarray(self.A, dtype=float) @ np.asarray(x, dtype=float)


def build_measurement_matrix(
    graph: TopologyGraph,
    device_ids: Sequence[str] | None = None,
    *,
    noise_floor_w: float = DEFAULT_NOISE_FLOOR_W,
    row_sigmas: Mapping[str, float] | None = None,
) -> MeasurementMatrix:
    """Build ``A`` for ``device_ids`` (default: every device in the topology)."""
    devices = list(dict.fromkeys(device_ids if device_ids is not None else graph.device_ids))
    dev_index = {d: i for i, d in enumerate(devices)}
    n = len(devices)

    # Which channels are informative?  Attribution CTs, their ancestors (for
    # mains), and any main CT containing a selected device.
    informative: set[str] = set()
    for did in devices:
        informative.update(graph.attribution_ct_ids(did))
    for cid in list(informative):
        for anc in graph.ancestors(cid):
            if graph.is_enabled(anc):
                informative.add(anc)
    for main in graph.main_channels():
        if any(did in _descendant_devices(graph, main.ct_id) for did in devices):
            informative.add(main.ct_id)

    rows = [
        cid
        for cid in graph.channel_ids
        if cid in informative and graph.is_enabled(cid)
    ]
    row_index = {cid: i for i, cid in enumerate(rows)}
    A = np.zeros((len(rows), n), dtype=float)
    row_phases: list[Phase] = []
    row_kind: dict[str, str] = {}
    row_notes: dict[str, list[str]] = {}
    sigmas = np.zeros(len(rows))
    nested: dict[str, tuple[str, ...]] = {}

    for i, cid in enumerate(rows):
        ch = graph.channels[cid]
        row_phases.append(Phase(ch.phase))
        row_kind[cid] = str(ch.ct_type)
        sigmas[i] = (row_sigmas or {}).get(cid, row_sigma(ch, noise_floor_w=noise_floor_w))

    for did in devices:
        j = dev_index[did]
        discipline = graph.resolve(did)
        attribution = graph.attribution_ct_ids(did)
        if not attribution:
            # No branch/dedicated CT: only main rows can observe it.
            for main in discipline.enclosing_main_ct_ids:
                if main in row_index:
                    phase = Phase(graph.channels[main].phase)
                    A[row_index[main], j] += graph.phase_coefficient(did, phase)
            continue
        # 1. Its own attribution rows (dedicated or deepest branch).
        for cid in attribution:
            if cid not in row_index:
                continue
            phase = Phase(graph.channels[cid].phase)
            coeff = _attribution_coefficient(graph, did, cid, attribution, phase)
            A[row_index[cid], j] += coeff
            _note(row_notes, cid, "attribution_row")
        # 2. Ancestor branch rows do NOT repeat the device; they subtract the
        #    child CT instead (nested_children below).
        # 3. Main rows contain the device via containment.
        for main in discipline.enclosing_main_ct_ids:
            if main not in row_index:
                continue
            phase = Phase(graph.channels[main].phase)
            A[row_index[main], j] += graph.phase_coefficient(did, phase)
            _note(row_notes, main, "containment_row")

    # Nested branch CT modelling: a parent branch row subtracts its direct
    # enabled children that are measured as rows.
    for cid in rows:
        ch = graph.channels[cid]
        if ch.ct_type != CTType.BRANCH:
            continue
        child_rows = tuple(
            child
            for child in graph.children.get(cid, ())
            if child in row_index and graph.is_enabled(child)
        )
        if child_rows:
            nested[cid] = child_rows
            _note(row_notes, cid, "nested_children:" + ",".join(child_rows))

    # Persist channel polarity so raw float readings can be corrected.
    mm = MeasurementMatrix(
        A=A,
        row_channel_ids=rows,
        col_device_ids=devices,
        row_weights=np.where(sigmas > 0, 1.0 / sigmas, 0.0),
        row_sigma=sigmas,
        row_phases=row_phases,
        row_kind=row_kind,
        row_notes=row_notes,
        nested_children=nested,
        channel_polarity={
            cid: str(graph.channels[cid].polarity) for cid in rows
        },
        channel_multiplier={cid: graph.multiplier_for(cid) for cid in rows},
        unobserved_devices=[d for d in devices if np.allclose(A[:, dev_index[d]], 0.0)],
        topology_version=graph.topology.version,
        phase_split_l1=graph.phase_split_l1,
    )
    return mm


def _note(notes: dict[str, list[str]], cid: str, note: str) -> None:
    notes.setdefault(cid, [])
    if note not in notes[cid]:
        notes[cid].append(note)


def _attribution_coefficient(
    graph: TopologyGraph,
    device_id: str,
    ct_id: str,
    attribution: Sequence[str],
    row_phase: Phase,
) -> float:
    """Coefficient of ``device_id`` in its own attribution row ``ct_id``.

    For a symmetric double-pole pair of branch/dedicated CTs each leg carries
    its own share of the total.  For any other CT the standard phase
    coefficient applies.
    """
    ch = graph.channels[ct_id]
    if ch.paired_ct_id and len(attribution) >= 2 and graph.device(device_id) is not None:
        dev_phase = graph.device_phase(device_id)
        if dev_phase == Phase.BOTH:
            if row_phase == Phase.BOTH:
                return 1.0
            if row_phase == Phase.L1:
                return graph.phase_split_l1
            if row_phase == Phase.L2:
                return 1.0 - graph.phase_split_l1
            return 0.0
    return graph.phase_coefficient(device_id, row_phase)


def _descendant_devices(graph: TopologyGraph, ct_id: str) -> set[str]:
    out: set[str] = set()
    for did in graph.device_ids:
        discipline = graph.resolve(did)
        if ct_id in discipline.enclosing_main_ct_ids:
            out.add(did)
    return out
