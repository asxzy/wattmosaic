"""Read-only Home Assistant WebSocket ingestion.

Design constraints (must remain true):

* **No service calls.**  The only commands issued are ``get_states``,
  ``config/*_registry/list`` and ``subscribe_events``.  There is deliberately no
  code path that can actuate a device.
* ``last_updated`` is a state-update timestamp, not proof of a physical
  measurement.  Every emitted ``Measurement`` carries ``state_timestamp`` and
  ``received_at`` separately, and identical numeric values are flagged
  ``repeated=True`` instead of being treated as fresh data.
* A malformed payload is logged and skipped; it can never break the event loop.
* Reconnection uses jittered exponential backoff from 1 s to 60 s, forever.

Reference implementation for the HA 2026.11 protocol:
``tools/audit/ha_client.py`` / ``tools/audit/collect_samples.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import json
import logging
import math
import random
import ssl
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol

try:  # websockets is a declared dependency, but ingestion must import safely.
    from websockets.exceptions import ConnectionClosed as _ConnectionClosed
except Exception:  # pragma: no cover - only if websockets is absent

    class _ConnectionClosed(Exception):  # type: ignore[no-redef]
        """Fallback so the module imports without websockets installed."""


from ..contracts import (
    DeviceEvent,
    Measurement,
    Polarity,
    QualityStatus,
    utcnow,
)
from .correlation import level_from_attributes
from .normalize import (
    canonical_unit,
    entity_allowed,
    is_own_entity,
    normalize_unit,
    parse_float,
    parse_state_timestamp,
)

log = logging.getLogger("wattmosaic.ingestion.ha")

BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0
BACKOFF_JITTER = 0.20
DEFAULT_STALE_AFTER_S = 120.0
MAX_FRAME_BYTES = 64 * 1024 * 1024

#: Longest window asked of the recorder in one ``history_during_period`` call
#: when attributes are requested.
#:
#: The cap is *bytes per frame*, and the recorder answers one request with one
#: frame, so the only way to stay under :data:`MAX_FRAME_BYTES` is to bound the
#: bytes a single request can produce.  Measured on the reference install (485
#: candidate entities plus the CT rows, ``include_attributes=True``) the payload
#: of a whole-candidate-set query was:
#:
#: ===========  =================
#: window       response
#: ===========  =================
#: 24 h          39.7 MB
#: 36 h          59.8 MB
#: 48 h          79.6 MB  -> dropped
#: 72 h         119.2 MB  -> dropped
#: 168 h        282.5 MB  -> dropped
#: ===========  =================
#:
#: So size grows linearly with the window at roughly 1.7 MB/h, and 48 h alone was
#: 1.24x over the 64 MiB cap.  Entity count is *not* a usable knob: the same 485
#: entities split into batches of 64 still produced a 266.8 MB frame, because a
#: handful of dense power rows dominate, and batches of 128/256 silently returned
#: 0 entities.  12 h was therefore chosen as the slice: on the same data a
#: 12 h slice peaked at **20.1 MB**, i.e. 3.2x headroom under the 64 MiB cap,
#: against 1.5x for the 24 h slice that already reached 41.5 MB.  A 16 h slice
#: would leave only ~2.4x; 12 h keeps a busy week (fixtures cycling far more
#: than the day measured here) well inside the limit while still collapsing a
#: 24 h request into two calls.
#:
#: This must never be confused with a target *size*: it is a time bound whose
#: measured consequence happens to stay small.  Raising :data:`MAX_FRAME_BYTES`
#: instead was rejected -- the cap is a real protection against a recorder that
#: streams thousands of rows, and a bigger cap only moves the cliff.
HISTORY_SLICE_HOURS = 12.0

#: Windows up to this long stay a single request.
#:
#: A short window is already far from the cap (24 h measured 39.7 MB, still one
#: frame) and the unsliced route costs ~4 s; splitting it would double the round
#: trips for no safety gain.  24 h is the largest window measured to fit *and*
#: the default lookback, so it is the natural threshold: at 24 h a single
#: request is provably safe today, and anything longer is sliced.
#:
#: Consequence worth stating: the largest frame the engine can ever ask for is
#: then the 24 h single request at 39.7 MB (1.6x headroom), because every longer
#: window is cut into 12 h slices whose measured peak is 20.1 MB.  That 1.6x is
#: the *pre-existing* default route, measured working on production; the slicing
#: exists so that no window can grow past it.  Lowering the threshold would buy
#: more headroom on the common case at the cost of a second round trip, which is
#: not worth it while the 24 h answer demonstrably fits.
HISTORY_SLICE_THRESHOLD_HOURS = 24.0

#: How far a slice's *left* edge is nudged earlier than its logical start.
#:
#: The recorder's SQL is ``last_updated_ts > start`` and ``last_updated_ts < end``
#: (see ``_significant_states_stmt`` in HA's ``recorder/history``), so a row
#: stamped exactly on a slice boundary belongs to neither neighbour: strict
#: ``< end`` excludes it from the slice before, strict ``> start`` from the slice
#: after.  A dropped transition at a seam is the same class of defect as a
#: duplicated one -- the correlation code counts transitions -- so each interior
#: slice asks from one microsecond before its logical start.  That makes the
#: logical ranges genuinely half-open ``[start, end)`` at the recorder's own
#: timestamp resolution (microseconds), with the instant owned by the later
#: slice.  One microsecond is smaller than any timestamp HA can store, so this
#: cannot pull in a row the previous slice already owns.
HISTORY_BOUNDARY_NUDGE = timedelta(microseconds=1)

#: Smallest per-slice timeout when a query is split.
#:
#: A sliced request is *smaller* than an unsliced one, so the caller's total
#: budget divided by the slice count is the natural share.  The floor exists so
#: a short window split into two slices does not get half of a budget that was
#: already only sized for one request.  A measured 12 h slice took 2.2 s, so 15 s
#: is nearly seven times the observed cost while keeping a 7-day query (14
#: slices, 420 s budget) at 30 s per slice.
HISTORY_SLICE_MIN_TIMEOUT_S = 15.0

READ_ONLY_COMMANDS = frozenset(
    {
        "get_states",
        "config/entity_registry/list",
        "config/device_registry/list",
        "config/area_registry/list",
        "config/floor_registry/list",
        "subscribe_events",
        "render_template",
        # Read-only recorder queries.  The first build of the CT/switch
        # correlation (ADR-0024) needs past transitions, not just the current
        # snapshot: a correlation cannot be established from one instant, and
        # waiting days for live transitions would leave the feature unusable on
        # a fresh installation.  Neither command writes anything.
        "history/history_during_period",
        "logbook/get_events",
    }
)


class ConnectionState(str, Enum):
    STOPPED = "stopped"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


SampleHandler = Callable[[Measurement], Any]
EventHandler = Callable[[DeviceEvent], Any]


def compute_backoff(
    attempt: int,
    *,
    base_s: float = BACKOFF_BASE_S,
    max_s: float = BACKOFF_MAX_S,
    jitter: float = BACKOFF_JITTER,
    rng: random.Random | None = None,
) -> float:
    """Jittered exponential backoff: ``base * 2**attempt`` capped at ``max_s``.

    ``attempt`` is zero-based (first reconnect after a failure waits ~1 s).
    Jitter is multiplicative and the result is capped so callers always see a
    bounded delay.  Deterministic when an ``rng`` is supplied.
    """
    attempt = max(0, int(attempt))
    generator = rng or random.Random()
    base = max(0.0, float(base_s))
    cap = max(0.0, float(max_s))
    if base <= 0 or cap <= 0:
        return 0.0
    # Clamp the exponent before shifting so huge attempt counts cannot overflow.
    # Once base * 2**attempt >= cap the result is capped anyway.
    try:
        max_exponent = math.log2(cap / base)
    except (ValueError, ZeroDivisionError):
        return cap
    exponent = min(attempt, max(0.0, math.ceil(max_exponent)) + 1.0)
    raw = min(cap, base * (2.0**exponent))
    factor = 1.0 + generator.uniform(-abs(jitter), abs(jitter))
    return max(0.05, min(cap, raw * factor))


def _ct_id_for(entity_id: str) -> str:
    """Stable CT id derived from the entity id (entity ids are unique)."""
    if "." in entity_id:
        return entity_id.split(".", 1)[1]
    return entity_id


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def history_window_slices(
    start: datetime,
    end: datetime,
    *,
    slice_hours: float = HISTORY_SLICE_HOURS,
    threshold_hours: float = HISTORY_SLICE_THRESHOLD_HOURS,
) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end)`` into recorder-safe windows.

    Returns ``[(start, end)]`` unchanged when the window is at most
    ``threshold_hours``: a single request is cheaper (the reference install's
    unsliced route costs ~4 s) and, up to 24 h, measured *safe* (39.7 MB against
    a 64 MiB frame cap).  Splitting a window that fits would double the round
    trips for no protection.

    Longer windows are cut into consecutive ``slice_hours`` pieces.  The pieces
    are returned as half-open ``[start, end)`` ranges; the caller turns that into
    the recorder's strict-SQL bounds.  There is no overlap and no gap, so the
    union is exactly the requested window.
    """
    if end <= start:
        # Reversed or empty: pass it through.  Home Assistant answers an empty
        # range with an empty result, and inventing slices for it would only
        # multiply a request that cannot return anything.
        return [(start, end)]
    if end - start <= timedelta(hours=threshold_hours):
        return [(start, end)]
    width = timedelta(hours=slice_hours)
    slices: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        stop = min(cursor + width, end)
        slices.append((cursor, stop))
        cursor = stop
    return slices


def _history_row_stamp(row: Mapping[str, Any]) -> float | None:
    """Epoch seconds of a compressed recorder row, or ``None`` if unparseable."""
    raw = row.get("lu", row.get("last_updated"))
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    parsed = parse_state_timestamp(raw)
    return parsed.timestamp() if parsed is not None else None


def merge_history_slices(parts: Iterable[Mapping[str, Any]]) -> dict[str, list[Any]]:
    """Concatenate per-slice recorder payloads into one payload.

    ``parts`` must be in chronological order.  Each entity's rows are appended in
    that order and each recorder answer is already sorted, so the merged list is
    ascending in time just like a single unsliced answer.

    **Seam-exact.**  A recorder row can appear in two adjacent slices: the
    recorder answers ``last_updated_ts > start`` and ``< end``, and a row at an
    instant returned by one slice can be returned again at the start of the next
    one (the recorder can synthesise a "state at the start of the window" row from
    the last state before it).  Emitting such a row twice would turn one
    transition into two -- a phantom on/off event -- and the correlation and
    group-fit code counts transitions, so a duplicated seam row would corrupt the
    evidence that placement is decided on.

    A row from a later slice is therefore admitted only when it is **strictly
    newer** than the youngest row already merged for that entity.  With the
    half-open bounds the recorder's own SQL produces, that filter should never
    drop a real row (a real row exactly on the seam belongs to the later slice
    only); it is defence in depth against a recorder that returns an overlapping
    row anyway.  Rows *inside* one slice are never filtered, so two distinct
    observations at the same instant are both kept.
    """
    merged: dict[str, list[Any]] = {}
    #: Youngest row admitted so far per entity (``None`` when unparseable).
    #: Only a slice boundary consults it, so a same-instant pair that genuinely
    #: lives inside one answer is preserved.
    youngest: dict[str, float | None] = {}

    for index, payload in enumerate(parts):
        for entity_id, rows in (payload or {}).items():
            if not isinstance(entity_id, str):
                continue
            bucket = merged.setdefault(entity_id, [])
            # Fixed for the whole slice: everything at or before the last row of
            # the previous slice is that slice's answer (including the start-time
            # state the recorder synthesises for this window), not new history.
            # Rows *inside* this slice are all admitted, so two real observations
            # at the same instant are not collapsed into one.
            boundary = youngest.get(entity_id) if index else None
            latest: float | None = None
            for row in rows if isinstance(rows, (list, tuple)) else ():
                if not isinstance(row, Mapping):
                    continue
                stamp = _history_row_stamp(row)
                if boundary is not None and stamp is not None and stamp <= boundary:
                    continue
                bucket.append(row)
                if stamp is not None:
                    latest = stamp
            if latest is not None:
                youngest[entity_id] = latest
            elif index:
                youngest.setdefault(entity_id, None)
    return merged


class HASource:
    """Home Assistant state ingestion over a single read-only WebSocket."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify_ssl: bool = True,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        allow_entities: Iterable[str] | None = None,
        deny_entities: Iterable[str] | None = None,
        include_energy: bool = True,
        emit_measurement_events: bool = False,
        request_timeout_s: float = 30.0,
        connect_factory: Callable[..., Any] | None = None,
        backoff_base_s: float = BACKOFF_BASE_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        backoff_jitter: float = BACKOFF_JITTER,
        rng: random.Random | None = None,
        stop_timeout_s: float = 10.0,
    ) -> None:
        if not token:
            raise ValueError("HASource requires a long-lived access token")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.verify_ssl = verify_ssl
        self.stale_after_s = float(stale_after_s)
        self.allow_entities = tuple(allow_entities or ())
        self.deny_entities = tuple(deny_entities or ())
        self.include_energy = include_energy
        self.emit_measurement_events = emit_measurement_events
        self.request_timeout_s = float(request_timeout_s)
        #: Why the most recent :meth:`history` call returned nothing, or ``None``
        #: when it returned a payload.  ``history`` deliberately returns an empty
        #: mapping on failure (a caller must never read failure as "no change"),
        #: but "the recorder is empty" and "the query timed out" are different
        #: facts and only this attribute distinguishes them.  Read it only after an
        #: empty result.
        self.last_history_error: str | None = None
        self._connect_factory = connect_factory
        self._backoff_base_s = backoff_base_s
        self._backoff_max_s = backoff_max_s
        self._backoff_jitter = backoff_jitter
        self._rng = rng or random.Random()
        self._stop_timeout_s = stop_timeout_s

        self._state = ConnectionState.STOPPED
        self._ws: WebSocketLike | None = None
        self._task: asyncio.Task[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._connected_event = asyncio.Event()
        self._background: set[asyncio.Task[Any]] = set()

        self._next_request_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}

        self._on_sample: SampleHandler | None = None
        self._on_event: EventHandler | None = None

        self._last_event_at: datetime | None = None
        self._last_error: str | None = None
        self._connected_since: datetime | None = None
        self._reconnect_attempt = 0

        self._sequence = 0
        self._last_numeric: dict[str, float] = {}
        self._unavailable: set[str] = set()

        self._states_cache: list[dict[str, Any]] = []
        self._registries_cache: dict[str, Any] = {}
        self._entity_meta: dict[str, dict[str, Any]] = {}

        self.stats: dict[str, int] = {
            "frames_received": 0,
            "events_received": 0,
            "samples_emitted": 0,
            "events_emitted": 0,
            "repeated_samples": 0,
            "own_entities_skipped": 0,
            "malformed_skipped": 0,
            "reconnects": 0,
            "callbacks_failed": 0,
        }

    # -- construction helpers -------------------------------------------- #
    def _is_own_output(self, entity_id: str) -> bool:
        """True when ``entity_id`` is one of WattMosaic's own entities.

        The registry ``platform`` is authoritative and is available for every
        entity after ``_prime``.  Before the registries load (the very first
        frames of a connection) only the id prefix is known, which is enough
        for the engine's own global sensors.
        """
        meta = self._entity_meta.get(entity_id) or {}
        return is_own_entity(entity_id, meta.get("platform"))

    @classmethod
    def from_config(cls, config: Any) -> HASource:
        return cls(
            config.ha_url,
            config.ha_token,
            verify_ssl=config.ha_verify_ssl,
            stale_after_s=config.stale_after_s,
        )

    # -- properties ------------------------------------------------------- #
    @property
    def connected(self) -> bool:
        return self._state is ConnectionState.CONNECTED

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def last_event_at(self) -> datetime | None:
        return self._last_event_at

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def connected_since(self) -> datetime | None:
        return self._connected_since

    @property
    def reconnect_attempt(self) -> int:
        return self._reconnect_attempt

    @property
    def unavailable_entities(self) -> frozenset[str]:
        return frozenset(self._unavailable)

    # -- lifecycle -------------------------------------------------------- #
    async def start(
        self,
        on_sample: SampleHandler | None = None,
        on_event: EventHandler | None = None,
    ) -> None:
        """Start the background ingestion task.

        Returns once the first connection attempt has been *scheduled*; use
        :meth:`wait_connected` (bounded) if the caller needs the first snapshot.
        """
        if self._task and not self._task.done():
            return
        if on_sample is not None:
            self._on_sample = on_sample
        if on_event is not None:
            self._on_event = on_event
        self._stopping = False
        self._state = ConnectionState.CONNECTING
        self._task = asyncio.create_task(self._run(), name="wattmosaic-ha-source")

    async def run_supervised(self) -> None:
        """Await the connection loop; used by the process supervisor."""
        await self.start()
        task = self._task
        if task is not None:
            await task

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        """Wait until the first successful subscription, bounded by ``timeout``."""
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def stop(self) -> None:
        """Stop ingestion and close the socket.  Safe to call repeatedly.

        Shutdown is intentionally silent: an intentional close must never log an
        ERROR or leave an unretrieved reader exception behind.
        """
        self._stopping = True
        self._state = ConnectionState.STOPPED
        task, self._task = self._task, None
        reader, self._reader_task = self._reader_task, None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("ingestion stopped"))
        self._pending.clear()
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), self._stop_timeout_s)
        if reader is not None:
            await self._cancel_task(reader)
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(task, self._stop_timeout_s)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        await self._close_background()

    async def _cancel_task(self, task: asyncio.Task[Any] | None) -> None:
        """Cancel a task and always retrieve its exception (no asyncio noise)."""
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - intentional shutdown
            pass

    # -- read-only queries ------------------------------------------------ #
    async def state_snapshot(self) -> list[dict[str, Any]]:
        """Return raw HA states (cached when the socket is down)."""
        if not self.connected:
            return list(self._states_cache)
        try:
            result = await self._request("get_states")
        except Exception as exc:  # noqa: BLE001
            log.warning("get_states failed: %s", exc)
            return list(self._states_cache)
        if isinstance(result, list):
            self._states_cache = result
            return list(result)
        return list(self._states_cache)

    async def history(
        self,
        entity_ids: Iterable[str],
        *,
        start: datetime,
        end: datetime | None = None,
        include_attributes: bool = False,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Recorder history for these entities, as HA returns it.

        Read-only, and the only way to see a transition that happened before the
        engine was running.  Returns an empty mapping when the recorder is not
        available or the query fails: the caller treats that as "no history",
        never as "no change", so a missing recorder cannot manufacture evidence.

        ``timeout_s`` overrides the connection's default request timeout.  A
        history query is the one command whose cost grows with *both* the window
        and the entity count, and it is the only caller that asks for days of
        data across every candidate.  On the reference install the whole-house
        query took 7 s at 24 h, 9 s at 48 h and 17 s at 72 h, so a 7-day request
        (485 entities) exceeded the 30 s default and raised
        ``asyncio.TimeoutError``.  That used to be swallowed by the ``except``
        below, and the caller then reported "no recorded history" -- a statement
        about the *recorder*, which was healthy, rather than about the query,
        which had not finished.  The caller passes a longer budget because it
        knows the window it asked for; validating the answer is still its job.

        ``include_attributes`` asks the recorder for attribute payloads.  Dimmer
        output lives in attributes, so learning a level→watts curve requires it;
        a plain switch correlation does not, and omitting them keeps the response
        small.  Note that many integrations store only ``device_id`` and
        ``friendly_name`` per row regardless, so an empty attribute payload is a
        normal answer and never an error.

        **Long windows are sliced.**  The recorder answers one request with one
        websocket frame, and :meth:`_connect` caps a frame at
        :data:`MAX_FRAME_BYTES` (64 MiB).  Above that the websockets library drops
        the connection, which is why every window longer than ~40 h used to fail
        with ``ConnectionError: websocket closed by peer``.  Measured on the
        reference install with ``include_attributes=True``: 24 h = 39.7 MB (one
        frame, safe), 48 h = 79.6 MB and 168 h = 282.5 MB (both over the cap and
        dropped).  Windows longer than :data:`HISTORY_SLICE_THRESHOLD_HOURS`
        (24 h, the largest measured to fit) are therefore split into slices of at
        most :data:`HISTORY_SLICE_HOURS` (12 h, whose measured peak frame was
        20.1 MB -- 3.2x headroom under the cap) and merged back into one mapping.

        The slices are half-open ``[start, end)`` at the recorder's microsecond
        resolution, and merging is seam-exact: a row the recorder returns at a
        slice boundary is kept exactly once.  A duplicated transition is a
        phantom on/off event, and the correlation/group code counts transitions,
        so merging must not invent one at every seam.

        A failure in *any* slice fails the whole call (empty mapping plus
        :attr:`last_history_error` naming the slice): returning the slices that
        did succeed would silently truncate the window while looking complete.
        """
        wanted = [str(entity_id) for entity_id in entity_ids if entity_id]
        if not wanted or not self.connected:
            return {}
        self.last_history_error = None
        end_time = _as_aware(end) or utcnow()
        start_time = _as_aware(start) or end_time
        slices = history_window_slices(start_time, end_time)
        base_timeout = self.request_timeout_s if timeout_s is None else float(timeout_s)
        # A slice is smaller than the whole query, so the caller's budget divided
        # across the slices is the natural share.  The floor keeps a short window
        # split into a handful of slices from getting less time than the single
        # request it replaced would have had.
        slice_timeout = base_timeout
        if len(slices) > 1:
            slice_timeout = max(HISTORY_SLICE_MIN_TIMEOUT_S, base_timeout / len(slices))

        parts: list[Any] = []
        for index, (slice_start, slice_end) in enumerate(slices, start=1):
            # The recorder's SQL is strict on both sides (``last_updated_ts >
            # start`` and ``< end``), so nudging the left edge back by a
            # microsecond is what makes the logical ranges meet with no gap and
            # no overlap -- see :data:`HISTORY_BOUNDARY_NUDGE`.  Exact-boundary
            # rows therefore belong to the later slice only.
            #
            # ``include_start_time_state`` is asked for on the *first* slice only.
            # It makes the recorder synthesise a row stamped at the query start
            # carrying the last state before it, which is how the unsliced call
            # knows what was on when the window opened.  For a later slice that
            # row would be stale: its value predates the previous slice's rows, so
            # a fixture that toggled exactly on the boundary would be replayed as
            # on -> off -> on and invent two transitions.  The previous slice
            # already supplies that context, so later slices ask for changes only.
            query_start = (
                slice_start if index == 1 else slice_start - HISTORY_BOUNDARY_NUDGE
            )
            try:
                result = await self._request(
                    "history/history_during_period",
                    start_time=query_start.isoformat(),
                    end_time=slice_end.isoformat(),
                    entity_ids=wanted,
                    include_start_time_state=index == 1,
                    minimal_response=not include_attributes,
                    no_attributes=not include_attributes,
                    significant_changes_only=False,
                    _timeout_s=slice_timeout,
                )
            except Exception as exc:  # noqa: BLE001 - recorder may not be enabled
                # Recorded *and* swallowed: the empty mapping below is the
                # caller's contract (failure is never evidence), but the reason is
                # kept so a caller can tell a timeout from a genuinely empty
                # recorder instead of reporting "no recorded history" for a query
                # that never finished.  Naming the slice is deliberate: "slice 3
                # of 7 failed" is a fact a user can act on, while a bare timeout
                # says nothing about how much of the window was answered.  An
                # unsliced window says "the whole window" instead of "slice 1 of
                # 1", because that is the request the user's window actually made.
                where = (
                    "the whole window"
                    if len(slices) == 1
                    else f"slice {index} of {len(slices)}"
                )
                self.last_history_error = (
                    f"{where} [{slice_start.isoformat()}, {slice_end.isoformat()}) "
                    f"failed: {type(exc).__name__}: {exc}"
                )
                log.debug("history query failed: %s", self.last_history_error)
                # Partial slices are *not* a partial answer: drop everything so
                # the caller cannot reason about a truncated window as if it were
                # the whole one.
                return {}
            parts.append(result if isinstance(result, dict) else {})

        if len(parts) == 1:
            return parts[0]
        return merge_history_slices(parts)

    async def registries(self) -> dict[str, Any]:
        """Return entity/device/area/floor registries (cached)."""
        if not self.connected:
            return dict(self._registries_cache)
        try:
            await self._load_registries()
        except Exception as exc:  # noqa: BLE001
            log.warning("registry refresh failed: %s", exc)
        return dict(self._registries_cache)

    # -- connection loop -------------------------------------------------- #
    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            self._state = ConnectionState.CONNECTING if attempt == 0 else ConnectionState.RECONNECTING
            try:
                await self._connect_once()
                # ``_connect_once`` only returns when the socket closes.
                if self._stopping:
                    break
                raise ConnectionError("websocket closed by peer")
            except asyncio.CancelledError:
                raise
            except _ConnectionClosed as exc:
                # A close while stopping is expected and must stay silent.
                if self._stopping:
                    break
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.info("Home Assistant closed the connection: %s", exc)
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                if self._stopping:
                    break
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.warning("HA connection error: %s", self._last_error)
            finally:
                self._teardown_connection()
            if self._stopping:
                break
            delay = compute_backoff(
                attempt,
                base_s=self._backoff_base_s,
                max_s=self._backoff_max_s,
                jitter=self._backoff_jitter,
                rng=self._rng,
            )
            attempt += 1
            self.stats["reconnects"] += 1
            self._reconnect_attempt = attempt
            self._state = ConnectionState.RECONNECTING
            log.info("reconnecting to HA in %.2fs (attempt %d)", delay, attempt)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    def _teardown_connection(self) -> None:
        self._state = ConnectionState.DISCONNECTED
        self._connected_since = None
        self._connected_event.clear()
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("connection lost"))
        self._pending.clear()
        ws, self._ws = self._ws, None
        if ws is not None:
            task = asyncio.create_task(self._safe_close(ws))
            # Track the task so it is always awaited/retrieved; otherwise asyncio
            # reports "Task exception was never retrieved" on shutdown.
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _close_background(self) -> None:
        for task in list(self._background):
            await self._cancel_task(task)
        self._background.clear()

    @staticmethod
    async def _safe_close(ws: WebSocketLike) -> None:
        with contextlib.suppress(Exception):
            await ws.close()

    def _ws_url(self) -> str:
        url = self.base_url
        if url.startswith("https://"):
            url = "wss://" + url[len("https://") :]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://") :]
        return f"{url}/api/websocket"

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self.base_url.startswith("https://"):
            return None
        ctx = ssl.create_default_context()
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _connect(self) -> Any:
        if self._connect_factory is not None:
            return self._connect_factory(self._ws_url())
        import websockets

        kwargs: dict[str, Any] = {
            "max_size": MAX_FRAME_BYTES,
            "ping_interval": 20,
            "ping_timeout": 20,
            "close_timeout": 10,
            "open_timeout": 15,
        }
        ctx = self._ssl_context()
        if ctx is not None:
            kwargs["ssl"] = ctx
        return websockets.connect(self._ws_url(), **kwargs)

    async def _connect_once(self) -> None:
        self._state = ConnectionState.CONNECTING
        async with self._connect() as ws:
            self._ws = ws
            self._state = ConnectionState.AUTHENTICATING
            await self._authenticate(ws)
            self._state = ConnectionState.CONNECTED
            self._connected_since = utcnow()
            self._reconnect_attempt = 0
            self._last_error = None
            log.info("connected to Home Assistant at %s", self.base_url)
            self._reader_task = asyncio.create_task(self._reader_loop(ws))
            try:
                await self._prime()
                await self._request("subscribe_events", event_type="state_changed")
                if self._stopping:
                    return
                self._connected_event.set()
                # Keep this coroutine alive until the reader dies or we stop.
                while not self._stopping:
                    if self._reader_task.done():
                        exc = (
                            None
                            if self._reader_task.cancelled()
                            else self._reader_task.exception()
                        )
                        if exc is not None:
                            raise exc
                        return
                    await asyncio.sleep(0.25)
            finally:
                reader, self._reader_task = self._reader_task, None
                await self._cancel_task(reader)

    async def _authenticate(self, ws: WebSocketLike) -> None:
        hello = await self._recv_json(ws)
        msg_type = hello.get("type")
        if msg_type == "auth_required":
            await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            auth = await self._recv_json(ws)
            if auth.get("type") != "auth_ok":
                raise PermissionError("Home Assistant authentication failed")
        elif msg_type == "auth_ok":  # trusted-network / already authenticated
            return
        elif msg_type == "auth_invalid":
            raise PermissionError("Home Assistant rejected the access token")
        else:
            raise ConnectionError(f"unexpected websocket handshake: {msg_type!r}")

    async def _recv_json(self, ws: WebSocketLike) -> dict[str, Any]:
        raw = await ws.recv()
        self.stats["frames_received"] += 1
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ConnectionError("invalid JSON frame from Home Assistant") from exc
        if not isinstance(msg, dict):
            raise ConnectionError("non-object websocket frame")
        return msg

    async def _prime(self) -> None:
        """Load registries and emit the initial state snapshot."""
        await self._load_registries()
        states = await self._request("get_states")
        if isinstance(states, list):
            self._states_cache = states
            for raw_state in states:
                try:
                    await self._handle_state(raw_state, initial=True)
                except Exception as exc:  # noqa: BLE001 - one bad state never hurts
                    self.stats["malformed_skipped"] += 1
                    log.warning("skipping malformed initial state: %s", exc)

    async def _load_registries(self) -> None:
        out: dict[str, Any] = {}
        for name, command in (
            ("entities", "config/entity_registry/list"),
            ("devices", "config/device_registry/list"),
            ("areas", "config/area_registry/list"),
            ("floors", "config/floor_registry/list"),
        ):
            try:
                result = await self._request(command)
                out[name] = result if result is not None else []
            except Exception as exc:  # noqa: BLE001 - registry is optional
                out[name] = {"error": type(exc).__name__}
                log.debug("registry %s unavailable: %s", name, exc)
        self._registries_cache = out
        self._entity_meta = self._build_entity_meta(out)

    @staticmethod
    def _build_entity_meta(registries: dict[str, Any]) -> dict[str, dict[str, Any]]:
        devices = {
            d.get("id"): d
            for d in registries.get("devices", [])
            if isinstance(d, dict) and d.get("id")
        }
        meta: dict[str, dict[str, Any]] = {}
        for entry in registries.get("entities", []):
            if not isinstance(entry, dict) or not entry.get("entity_id"):
                continue
            device = devices.get(entry.get("device_id"), {})
            meta[entry["entity_id"]] = {
                "device_id": entry.get("device_id"),
                "device_name": device.get("name_by_user") or device.get("name"),
                "name": entry.get("name") or entry.get("original_name"),
                "entity_category": entry.get("entity_category"),
                "disabled_by": entry.get("disabled_by"),
                "platform": entry.get("platform"),
            }
        return meta

    # -- request/response ------------------------------------------------- #
    async def _request(
        self, msg_type: str, *, _timeout_s: float | None = None, **kwargs: Any
    ) -> Any:
        if msg_type not in READ_ONLY_COMMANDS:
            # Defensive: the engine is read-only by construction.
            raise PermissionError(f"command {msg_type!r} is not permitted (read-only engine)")
        ws = self._ws
        if ws is None:
            raise ConnectionError("not connected")
        req_id = self._next_request_id
        self._next_request_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut
        # ``_timeout_s`` is a transport concern, not part of the HA command; it is
        # consumed here so it can never reach ``payload``.
        payload = {"id": req_id, "type": msg_type, **kwargs}
        await ws.send(json.dumps(payload))
        try:
            timeout = self.request_timeout_s if _timeout_s is None else float(_timeout_s)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(req_id, None)

    async def _reader_loop(self, ws: WebSocketLike) -> None:
        while True:
            try:
                msg = await self._recv_json(ws)
            except _ConnectionClosed as exc:
                # A close during shutdown is expected; do not surface it as an
                # error.  A close while running is reported by ``_run``.
                if not self._stopping:
                    log.info("Home Assistant websocket closed: %s", exc)
                return
            except asyncio.CancelledError:
                raise
            self.stats["frames_received"] += 1
            try:
                await self._dispatch(msg)
            except Exception as exc:  # noqa: BLE001 - malformed payload isolation
                self.stats["malformed_skipped"] += 1
                log.warning("skipping malformed HA frame: %s", exc)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "result":
            req_id = msg.get("id")
            fut = self._pending.get(req_id) if isinstance(req_id, int) else None
            if fut is None or fut.done():
                return
            if msg.get("success"):
                fut.set_result(msg.get("result"))
            else:
                fut.set_exception(ConnectionError(f"HA command failed: {msg.get('error')}"))
            return
        if msg_type != "event":
            return
        event = msg.get("event")
        if not isinstance(event, dict):
            return
        self._last_event_at = parse_state_timestamp(event.get("time_fired")) or utcnow()
        if event.get("event_type") != "state_changed":
            return
        self.stats["events_received"] += 1
        data = event.get("data")
        if not isinstance(data, dict):
            self.stats["malformed_skipped"] += 1
            return
        await self._handle_event_data(data, event.get("time_fired"))

    async def _handle_event_data(self, data: dict[str, Any], fired_at: Any) -> None:
        entity_id = data.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            self.stats["malformed_skipped"] += 1
            return
        new_state = data.get("new_state")
        old_state = data.get("old_state")
        if new_state is None:
            # Entity removed; surface availability loss as an event.
            self._unavailable.add(entity_id)
            await self._emit_device_event(
                entity_id, old_state, None, parse_state_timestamp(fired_at) or utcnow()
            )
            return
        if not isinstance(new_state, dict):
            self.stats["malformed_skipped"] += 1
            return
        await self._process_state(entity_id, new_state, old_state, fired_at)

    async def _handle_state(self, raw_state: dict[str, Any], *, initial: bool = False) -> None:
        entity_id = raw_state.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            raise ValueError("state without entity_id")
        await self._process_state(entity_id, raw_state, None, raw_state.get("last_updated"), initial=initial)

    async def _process_state(
        self,
        entity_id: str,
        new_state: dict[str, Any],
        old_state: dict[str, Any] | None,
        fired_at: Any,
        *,
        initial: bool = False,
    ) -> None:
        if not entity_allowed(entity_id, allow=self.allow_entities, deny=self.deny_entities):
            return
        if self._is_own_output(entity_id):
            # Never ingest WattMosaic's own estimates as if they were physical
            # measurements: that feeds predictions back into the model and
            # makes the output look like independent evidence.
            self.stats["own_entities_skipped"] += 1
            return
        attributes = new_state.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}

        raw_value = new_state.get("state")
        value_text = raw_value.strip().lower() if isinstance(raw_value, str) else ""
        if value_text in ("unavailable", "unknown", "none", ""):
            self._unavailable.add(entity_id)
            await self._emit_device_event(
                entity_id,
                old_state,
                new_state,
                parse_state_timestamp(new_state.get("last_updated"))
                or parse_state_timestamp(fired_at)
                or utcnow(),
                event_type="availability",
            )
            return
        self._unavailable.discard(entity_id)

        unit = canonical_unit(attributes.get("unit_of_measurement"))
        device_class = attributes.get("device_class")
        numeric = parse_float(raw_value)

        is_power = unit in ("W", "kW") or device_class == "power"
        is_energy = unit in ("kWh", "Wh") or device_class == "energy"
        if numeric is not None and (is_power or (is_energy and self.include_energy)):
            emitted = await self._emit_measurement(
                entity_id, numeric, unit, attributes, new_state, fired_at, initial=initial
            )
            if emitted and not self.emit_measurement_events:
                return

        # Non-measurement (state-bearing) entities produce device events.
        previous_state = old_state.get("state") if isinstance(old_state, dict) else None
        changed = previous_state != raw_value
        # A brightness change on a light that stays ``on`` is a real observation
        # of how much power the fixture is drawing now.  Dropping it as
        # "attribute churn" is why a dimmer's level was invisible to inference:
        # the row moved, the state did not, and nothing recorded why.  The event
        # still carries the same state on both sides; only the attributes differ,
        # which is exactly the information being preserved.
        level_changed = not changed and self._level_changed(old_state, new_state)
        if changed or level_changed or initial:
            await self._emit_device_event(
                entity_id,
                old_state,
                new_state,
                parse_state_timestamp(new_state.get("last_updated"))
                or parse_state_timestamp(fired_at)
                or utcnow(),
            )

    @staticmethod
    def _level_changed(
        old_state: dict[str, Any] | None, new_state: dict[str, Any] | None
    ) -> bool:
        """True when only the reported output level moved.

        Compared as parsed fractions, not raw attribute numbers, so a change of
        representation (``brightness: 255`` → ``brightness_pct: 100``) is not two
        events about the same 100 %.  Either side missing a level yields ``False``:
        "was not reported" is not a change to a known value.
        """
        if not isinstance(old_state, dict) or not isinstance(new_state, dict):
            return False
        before = level_from_attributes(old_state.get("attributes"))
        after = level_from_attributes(new_state.get("attributes"))
        return before is not None and after is not None and before != after

    async def _emit_measurement(
        self,
        entity_id: str,
        numeric: float,
        unit: str | None,
        attributes: dict[str, Any],
        new_state: dict[str, Any],
        fired_at: Any,
        *,
        initial: bool,
    ) -> bool:
        try:
            value, base_unit = normalize_unit(numeric, unit or ("kWh" if attributes.get("device_class") == "energy" else "W"))
        except ValueError as exc:
            self.stats["malformed_skipped"] += 1
            log.warning("skipping %s: %s", entity_id, exc)
            return False

        state_ts = (
            parse_state_timestamp(new_state.get("last_updated"))
            or parse_state_timestamp(new_state.get("last_changed"))
            or parse_state_timestamp(fired_at)
            or utcnow()
        )
        received_at = utcnow()
        age = max(0.0, (received_at - state_ts).total_seconds())
        quality = QualityStatus.STALE if age > self.stale_after_s else QualityStatus.GOOD

        repeated = self._last_numeric.get(entity_id) == value
        self._last_numeric[entity_id] = value
        if repeated:
            self.stats["repeated_samples"] += 1

        self._sequence += 1
        measurement = Measurement(
            entity_id=entity_id,
            ct_id=_ct_id_for(entity_id),
            value=value,
            unit=base_unit,  # type: ignore[arg-type]
            value_w=value if base_unit == "W" else None,
            state_timestamp=state_ts,
            received_at=received_at,
            quality=quality,
            repeated=repeated,
            polarization=Polarity.NORMAL,
            sequence=self._sequence,
        )
        self.stats["samples_emitted"] += 1
        await self._call(self._on_sample, measurement, "sample")
        return True

    async def _emit_device_event(
        self,
        entity_id: str,
        old_state: dict[str, Any] | None,
        new_state: dict[str, Any] | None,
        occurred_at: datetime,
        *,
        event_type: str = "state_changed",
    ) -> None:
        meta = self._entity_meta.get(entity_id, {})
        device_id = meta.get("device_id") or entity_id
        new_value = new_state.get("state") if isinstance(new_state, dict) else None
        old_value = old_state.get("state") if isinstance(old_state, dict) else None
        if (
            new_value == old_value
            and event_type == "state_changed"
            and not self._level_changed(old_state, new_state)
        ):
            # Attribute-only update with no change in runtime *or* output level:
            # not an event.  A changed level is kept, because it is the only way
            # inference learns how much a dimmer draws after it has been dimmed.
            return
        attributes = new_state.get("attributes") if isinstance(new_state, dict) else None
        event = DeviceEvent(
            event_id=str(uuid.uuid4()),
            device_id=device_id,
            entity_id=entity_id,
            event_type=event_type,  # type: ignore[arg-type]
            previous_state=None if old_value is None else str(old_value),
            new_state=None if new_value is None else str(new_value),
            attributes=attributes if isinstance(attributes, dict) else {},
            occurred_at=occurred_at,
            received_at=utcnow(),
        )
        self.stats["events_emitted"] += 1
        await self._call(self._on_event, event, "event")

    async def _call(self, callback: SampleHandler | EventHandler | None, payload: Any, kind: str) -> None:
        if callback is None:
            return
        try:
            result = callback(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - consumer errors must not kill ingestion
            self.stats["callbacks_failed"] += 1
            log.exception("ingestion %s callback failed", kind)


async def smoke_test(seconds: float = 30.0) -> dict[str, Any]:
    """Manual, passive smoke test against the configured real HA.

    Read-only: subscribes to state changes and counts normalized samples.  It
    never issues a service call and never writes to the database.
    """

    from ..config.settings import EngineConfig

    config = EngineConfig.from_env()
    problems = [p for p in config.validate() if "token" not in p]
    if problems:
        raise SystemExit("configuration invalid: " + "; ".join(problems))

    samples: list[Measurement] = []
    events: list[DeviceEvent] = []

    async def on_sample(m: Measurement) -> None:
        if len(samples) < 10:
            samples.append(m)

    async def on_event(e: DeviceEvent) -> None:
        if len(events) < 10:
            events.append(e)

    source = HASource.from_config(config)
    await source.start(on_sample=on_sample, on_event=on_event)
    connected = await source.wait_connected(timeout=20.0)
    await asyncio.sleep(max(1.0, seconds))
    result = {
        "connected": connected,
        "ha_url": config.ha_url,
        "last_event_at": source.last_event_at.isoformat() if source.last_event_at else None,
        "stats": dict(source.stats),
        "sample_examples": [m.model_dump(mode="json") for m in samples],
        "event_examples": [e.model_dump(mode="json") for e in events],
    }
    await source.stop()
    return result


def _main() -> None:
    parser = argparse.ArgumentParser(description="Passive WattMosaic HA ingestion smoke test (read-only)")
    parser.add_argument("--seconds", type=float, default=30.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(asyncio.run(smoke_test(args.seconds)), indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover - manual tool
    _main()


__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_MAX_S",
    "ConnectionState",
    "HASource",
    "READ_ONLY_COMMANDS",
    "compute_backoff",
    "smoke_test",
]
