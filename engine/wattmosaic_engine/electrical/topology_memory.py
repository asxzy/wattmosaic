"""Long structural memory of the T5 ledger: a compact, persisted aligned grid (design §5 T5).

The structural pass (:mod:`.topology_resolve`) needs days, not hours, of
aligned absolute values: a 240 V load that runs 3 % of the week shows its
capacity violation only while it runs, and a busy feed's differenced regression
separates two sibling parents only over thousands of steps.  The live runtime
used to align the last 6 h of 10 s buckets, so both were invisible to it and
every structural decision had to come from a Discover run.

:class:`StructureMemory` keeps one value per reader per
:data:`MEMORY_STEP_S` bucket (the last sample in the bucket; a quiet reader is
held, recorder semantics) for :data:`MEMORY_WINDOW_S`, in flat ``array('d')``
buffers (8 bytes a sample: a week of 40 readers is ~6 MB).  The 30 s grid
keeps the structural fit unchanged: a 30 s difference is what the 10 s grid
fit used (:data:`~.topology_resolve.STRUCTURE_DIFF_SPAN_S`).  The memory
survives restarts (:meth:`to_json` / :meth:`from_json`, delta-encoded and
zlib-compressed) and can be seeded from a history replay (:meth:`seed`).

Pure: no clock, no I/O; timestamps are POSIX seconds supplied by the caller.
``nan`` is absent (never 0 W); a restart gap is absent, never held.
"""

from __future__ import annotations

import base64
import json
import math
import threading
import zlib
from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .topology_resolve import AlignedSeries

__all__ = ["MEMORY_STEP_S", "MEMORY_WINDOW_S", "StructureMemory"]


def _as_floats(values: Sequence[float | None]) -> np.ndarray:
    if isinstance(values, np.ndarray):
        out = values.astype(np.float64, copy=True)
    else:
        out = np.array([np.nan if v is None else v for v in values], dtype=np.float64)
    out[~np.isfinite(out)] = np.nan
    return out

#: Grid step of the long structural memory.
MEMORY_STEP_S = 30.0
#: How far back it reaches (a week, like a 168 h Discover).
MEMORY_WINDOW_S = 7 * 24 * 3600.0
#: Trim the front in batches (deleting from an array's front is O(n)).
_TRIM_SLACK = 512
#: Persisted values are rounded to 0.1 W (delta-encoded int64).
_SCALE = 10.0
_ABSENT = -(2**62)
_FORMAT = 1


@dataclass
class _Track:
    start: int  # bucket index of values[0]
    values: array = field(default_factory=lambda: array("d"))
    #: The next gap is a restart (absent), not a quiet reader (held).
    resumed: bool = False

    @property
    def last(self) -> int:
        return self.start + len(self.values) - 1


class StructureMemory:
    """Per-reader aligned buckets over a long window (O(1) amortised per sample)."""

    def __init__(self, *, step_s: float = MEMORY_STEP_S, window_s: float = MEMORY_WINDOW_S) -> None:
        if step_s <= 0 or window_s < step_s:
            raise ValueError("step_s must be positive and window_s at least one step")
        self.step_s = float(step_s)
        self.window_s = float(window_s)
        self.capacity = int(window_s // step_s) + 1
        self._tracks: dict[str, _Track] = {}
        # The fast path records while a worker thread seeds, reads or saves:
        # every touch of ``_tracks`` holds this lock, and only for O(1) work or
        # a memcpy (ADR-0033: the fast path never waits on a long hold).
        self._lock = threading.Lock()

    # -- writes ---------------------------------------------------------------- #
    def record(self, reader: str, ts: float, value: float | None) -> None:
        """Keep the last sample of ``reader``'s bucket at ``ts`` (late samples are dropped)."""
        if not math.isfinite(ts):
            return
        bucket = int(ts // self.step_s)
        v = float(value) if value is not None and math.isfinite(float(value)) else math.nan
        with self._lock:
            self._record(reader, bucket, v)

    def _record(self, reader: str, bucket: int, v: float) -> None:
        track = self._tracks.get(reader)
        if track is None:
            self._tracks[reader] = _Track(start=bucket, values=array("d", [v]))
            return
        last = track.last
        if bucket < last:
            return  # the memory only moves forward
        if bucket == last:
            track.values[-1] = v
            return
        gap = bucket - last - 1
        if gap > 0:
            if gap >= self.capacity:
                # Nothing of the old track would survive: restart it.
                track.start, track.values, track.resumed = bucket, array("d", [v]), False
                return
            fill = math.nan if track.resumed else track.values[-1]
            track.values.extend(array("d", [fill]) * gap)
        track.resumed = False
        track.values.append(v)
        self._trim(track)

    def _trim(self, track: _Track) -> None:
        excess = len(track.values) - self.capacity
        if excess > _TRIM_SLACK:
            del track.values[:excess]
            track.start += excess

    def mark_resumed(self) -> None:
        """The process restarted: the gap before each reader's next sample is absent."""
        with self._lock:
            for track in self._tracks.values():
                track.resumed = True

    def seed(self, series: AlignedSeries) -> int:
        """Fill buckets *before* each reader's memory from an aligned history.

        ``series`` must be on this memory's grid (``step_s``) with a
        ``start_ts``; index ``i`` is the value at ``start_ts + i * step_s``,
        i.e. the last sample of the bucket before that instant.  Only the part
        older than what the memory already holds is used (live samples win).
        Returns the number of buckets added.
        """
        if series.start_ts is None or abs(series.step_s - self.step_s) > 1e-9:
            raise ValueError("seed series must be on the memory grid with a start_ts")
        first = round(series.start_ts / self.step_s) - 1
        prepared: dict[str, array] = {}
        for reader, values in series.items():
            row = _as_floats(values)
            if len(row):
                prepared[reader] = array("d", row.tobytes())
        added = 0
        with self._lock:
            for reader, filled in prepared.items():
                track = self._tracks.get(reader)
                if track is None:
                    self._tracks[reader] = track = _Track(start=first, values=filled)
                    added += len(filled)
                else:
                    keep = min(len(filled), track.start - first)
                    if keep <= 0:
                        continue
                    head = filled[:keep]
                    if first + keep < track.start:
                        head.extend(array("d", [math.nan]) * (track.start - first - keep))
                    head.extend(track.values)
                    track.start, track.values = first, head
                    added += keep
                self._trim(track)
        return added

    # -- reads ----------------------------------------------------------------- #
    def readers(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._tracks))

    def series(self, now_ts: float, *, window_s: float | None = None) -> AlignedSeries:
        """Every reader on one grid ending at ``now_ts`` (absent = ``nan``).

        A reader's last value is held up to ``now_ts`` (it reports on change);
        before its first sample it is absent.  Values are float64 arrays.
        """
        window = self.window_s if window_s is None else min(float(window_s), self.window_s)
        end = int(now_ts // self.step_s)
        with self._lock:
            tracks = {reader: t for reader, t in self._tracks.items() if t.values}
            if not tracks:
                return AlignedSeries({}, step_s=self.step_s, start_ts=None)
            first = max(min(t.start for t in tracks.values()), end - int(window // self.step_s))
            length = max(0, end - first + 1)
            out = {reader: self._row(tracks[reader], first, length) for reader in sorted(tracks)}
        return AlignedSeries(out, step_s=self.step_s, start_ts=(first + 1) * self.step_s)

    @staticmethod
    def _row(track: _Track, first: int, length: int) -> np.ndarray:
        row = np.full(length, np.nan)
        if length == 0 or not track.values:
            return row
        values = np.frombuffer(track.values, dtype=np.float64)
        lo = max(first, track.start)
        hi = min(first + length - 1, track.last)
        if lo <= hi:
            row[lo - first : hi - first + 1] = values[lo - track.start : hi - track.start + 1]
        if track.last < first + length - 1 and not track.resumed:
            row[max(track.last + 1, first) - first :] = values[-1]  # held up to now
        return row

    # -- persistence ----------------------------------------------------------- #
    def to_json(self) -> str:
        with self._lock:
            snapshot = {
                reader: (track.start, np.frombuffer(track.values, dtype=np.float64).copy())
                for reader, track in self._tracks.items()
                if track.values
            }
        tracks: dict[str, Any] = {}
        for reader, (start, values) in sorted(snapshot.items()):
            scaled = np.where(np.isfinite(values), np.round(values * _SCALE), _ABSENT).astype(np.int64)
            deltas = np.diff(scaled, prepend=np.int64(0))
            packed = base64.b64encode(zlib.compress(deltas.astype("<i8").tobytes(), 6)).decode("ascii")
            tracks[reader] = {"start": start, "n": len(values), "v": packed}
        return json.dumps(
            {"format": _FORMAT, "step_s": self.step_s, "tracks": tracks},
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(
        cls, text: str | None, *, step_s: float = MEMORY_STEP_S, window_s: float = MEMORY_WINDOW_S
    ) -> StructureMemory:
        """Restore a memory; an unreadable or foreign payload gives an empty one.

        A restored memory is :meth:`mark_resumed`: the downtime is absent.
        """
        memory = cls(step_s=step_s, window_s=window_s)
        try:
            payload = json.loads(text) if text else {}
        except (TypeError, ValueError):
            return memory
        if (
            not isinstance(payload, dict)
            or payload.get("format") != _FORMAT
            or payload.get("step_s") != memory.step_s
        ):
            return memory
        for reader, entry in (payload.get("tracks") or {}).items():
            try:
                raw = zlib.decompress(base64.b64decode(entry["v"]))
                scaled = np.cumsum(np.frombuffer(raw, dtype="<i8"))
                if len(scaled) != int(entry["n"]):
                    continue
                values = np.where(scaled == _ABSENT, np.nan, scaled / _SCALE)
                track = _Track(start=int(entry["start"]), values=array("d", values.tolist()), resumed=True)
            except Exception:  # noqa: BLE001 - one bad track must not drop the rest
                continue
            memory._tracks[str(reader)] = track
            memory._trim(track)
        return memory
