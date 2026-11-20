"""Discover must not stall the event loop (ADR-0033).

Measured on a live install during a 168 h Discover (40 readers, ~1.08 M
recorder rows): ``GET /api/v1/health`` through the add-on gateway timed out at
its 4 s budget once or twice per run.  A py-spy GIL profile showed two causes:

* on the event-loop thread, ``json.loads`` of ~20 MB recorder frames and
  ``merge_history_slices`` over ~1 M rows -- CPU work on the loop itself;
* on the ``_analyse`` worker thread, a per-row ``dict(row)`` copy of every
  recorder row handed to the group fit, holding the GIL for long stretches.

These tests pin the fixes: large frames and the merge run on a worker thread,
the fit inputs are passed by reference (and the copy loop yields the GIL), and
an asyncio ticker keeps being scheduled while a Discover-sized analysis runs.
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from collections.abc import Mapping
from typing import Any

import pytest
from wattmosaic_engine.core import discovery_api
from wattmosaic_engine.core.discovery_api import _fit_rows, _is_numeric_state
from wattmosaic_engine.ingestion import ha_source
from wattmosaic_engine.ingestion.correlation import parse_history_payload
from wattmosaic_engine.ingestion.ha_source import (
    LARGE_FRAME_BYTES,
    HASource,
    merge_history_slices,
)

T0 = 1.7e9
HOURS = 168.0
READERS = 40
ENTITIES = 60
#: ~1.08 M rows in total, the size of the live 168 h Discover.
ROWS_PER_READER = 27_000


def _synthetic_payload(seed: int = 7) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """A Discover-sized recorder answer: dense CT rows plus toggling fixtures."""
    rng = random.Random(seed)
    span = HOURS * 3600.0
    payload: dict[str, list[dict[str, Any]]] = {}
    entities = [f"light.fixture_{index}" for index in range(ENTITIES)]
    for entity in entities:
        rows: list[dict[str, Any]] = [{"s": "off", "lu": T0, "a": {}}]
        stamp, on = T0, False
        while True:
            stamp += rng.uniform(600.0, 7200.0)
            if stamp >= T0 + span:
                break
            on = not on
            rows.append({"s": "on" if on else "off", "lu": stamp, "a": {"friendly_name": entity}})
        payload[entity] = rows
    step = span / ROWS_PER_READER
    for reader in range(READERS):
        rows = [
            {"s": f"{50.0 + rng.gauss(0.0, 0.5):.2f}", "lu": T0 + k * step + 0.5, "a": {}}
            for k in range(ROWS_PER_READER)
        ]
        # A bridge restart: non-numeric rows the fit must never see.
        rows[100]["s"] = "unavailable"
        rows[200]["s"] = "nan"
        payload[f"sensor.ct_{reader}_power"] = rows
    return payload, entities


def _slices(payload: Mapping[str, list[dict[str, Any]]], count: int) -> list[dict[str, Any]]:
    """Split a payload into ``count`` chronological slices (the 12 h slicing)."""
    width = HOURS * 3600.0 / count
    parts: list[dict[str, Any]] = [{} for _ in range(count)]
    for entity, rows in payload.items():
        for row in rows:
            index = min(count - 1, int((row["lu"] - T0) // width))
            parts[index].setdefault(entity, []).append(row)
    return parts


@pytest.fixture(scope="module")
def discover_payload() -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    return _synthetic_payload()


# --------------------------------------------------------------------------- #
# Fit inputs: same rows, no copies
# --------------------------------------------------------------------------- #


def test_fit_rows_matches_the_old_copying_filter() -> None:
    rows: list[Any] = [
        {"s": "12.5", "lu": 1.0},
        {"s": "unavailable", "lu": 2.0},
        "not a row",
        {"s": 7, "lu": 3.0},
        {"s": float("inf"), "lu": 4.0},
        {"s": "nan", "lu": 5.0},
        {"state": " 3.0 ", "last_updated": 6.0},
        {"s": True, "lu": 7.0},
        {"lu": 8.0},
    ]
    old_numeric = [
        dict(row) for row in rows if isinstance(row, Mapping) and _is_numeric_state(row)
    ]
    old_all = [dict(row) for row in rows if isinstance(row, Mapping)]

    numeric = _fit_rows(rows, numeric_only=True)
    every = _fit_rows(rows)

    assert numeric == old_numeric
    assert [row["lu" if "lu" in row else "last_updated"] for row in numeric] == [1.0, 3.0, 6.0]
    assert every == old_all
    # Passed by reference: the recorder rows themselves, not copies.
    assert all(any(kept is row for row in rows) for kept in every)


def test_fit_rows_tolerates_non_list_input() -> None:
    assert _fit_rows(()) == []
    assert _fit_rows(iter([{"s": "1", "lu": 1.0}])) == [{"s": "1", "lu": 1.0}]
    assert _fit_rows({"s": "1"}) == []  # a dict iterates its keys: no rows


def test_fit_rows_yields_the_gil_on_long_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []
    monkeypatch.setattr(discovery_api.time, "sleep", lambda s: calls.append(s))
    rows = [{"s": "1", "lu": float(i)} for i in range(3 * discovery_api.FIT_ROWS_YIELD_EVERY + 1)]
    assert len(_fit_rows(rows, numeric_only=True)) == len(rows)
    assert calls == [0, 0, 0]


def test_fit_input_benchmark(
    discover_payload: tuple[dict[str, list[dict[str, Any]]], list[str]],
) -> None:
    """Before/after timing of the group-fit input preparation at 168 h scale."""
    payload, entities = discover_payload
    wanted = set(entities)
    cts = [entity for entity in payload if entity.startswith("sensor.")]

    started = time.perf_counter()
    old_rows = {
        ct: [dict(row) for row in payload.get(ct, ()) if isinstance(row, Mapping) and _is_numeric_state(row)]
        for ct in cts
    }
    old_states = {
        entity: [dict(row) for row in rows if isinstance(row, Mapping)]
        for entity, rows in payload.items()
        if entity in wanted
    }
    before = time.perf_counter() - started

    started = time.perf_counter()
    new_rows = {ct: _fit_rows(payload.get(ct, ()), numeric_only=True) for ct in cts}
    new_states = {
        entity: _fit_rows(rows) for entity, rows in payload.items() if entity in wanted
    }
    after = time.perf_counter() - started

    total = sum(len(rows) for rows in payload.values())
    print(f"\nfit inputs over {total} rows: before {before:.3f} s, after {after:.3f} s")
    assert new_rows == old_rows
    assert new_states == old_states
    assert all(len(rows) == ROWS_PER_READER - 2 for rows in new_rows.values())
    # Generous: the point is "not slower", and an absolute ceiling for CI.
    assert after < max(before * 1.5, 0.5)
    assert after < 15.0


# --------------------------------------------------------------------------- #
# The event loop keeps ticking while a Discover-sized analysis runs
# --------------------------------------------------------------------------- #


async def test_loop_keeps_ticking_during_discover_sized_analysis(
    discover_payload: tuple[dict[str, list[dict[str, Any]]], list[str]],
) -> None:
    payload, entities = discover_payload
    parts = _slices(payload, 14)
    wanted = set(entities)
    done = threading.Event()

    def _analysis() -> dict[str, Any]:
        try:
            merged = merge_history_slices(parts)
            parse_history_payload(merged)
            rows = {
                entity: _fit_rows(merged[entity], numeric_only=True)
                for entity in merged
                if entity.startswith("sensor.")
            }
            states = {entity: _fit_rows(merged[entity]) for entity in merged if entity in wanted}
            return {"merged": merged, "rows": rows, "states": states}
        finally:
            done.set()

    gaps: list[float] = []

    async def _ticker() -> None:
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.05)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(_ticker())
    started = time.perf_counter()
    result = await asyncio.to_thread(_analysis)
    elapsed = time.perf_counter() - started
    await ticker

    assert sum(len(rows) for rows in result["merged"].values()) == sum(
        len(rows) for rows in payload.values()
    )
    worst = max(gaps)
    print(
        f"\nDiscover-sized analysis {elapsed:.2f} s in a thread; "
        f"{len(gaps)} ticks, max loop gap {worst * 1000:.0f} ms"
    )
    assert gaps, "the ticker never ran while the analysis was in progress"
    assert worst < 1.0


# --------------------------------------------------------------------------- #
# HASource: big frames and the slice merge stay off the loop
# --------------------------------------------------------------------------- #


class _OneFrame:
    def __init__(self, raw: str) -> None:
        self.raw = raw

    async def recv(self) -> str:
        return self.raw

    async def send(self, message: str) -> None:  # pragma: no cover - unused
        pass

    async def close(self) -> None:  # pragma: no cover - unused
        pass


def _source() -> HASource:
    return HASource("http://example.invalid", "t")


async def test_large_frames_decode_on_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    offloaded: list[Any] = []
    real = asyncio.to_thread

    async def _spy(func: Any, *args: Any, **kwargs: Any) -> Any:
        offloaded.append(func)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(ha_source.asyncio, "to_thread", _spy)
    source = _source()

    small = {"type": "event", "id": 1}
    assert await source._recv_json(_OneFrame(json.dumps(small))) == small
    assert offloaded == []

    big = {"type": "result", "id": 2, "result": {"x": "y" * LARGE_FRAME_BYTES}}
    assert await source._recv_json(_OneFrame(json.dumps(big))) == big
    assert offloaded == [json.loads]

    with pytest.raises(ConnectionError):
        await source._recv_json(_OneFrame("{\"type\": " + "x" * LARGE_FRAME_BYTES))
    with pytest.raises(ConnectionError):
        await source._recv_json(_OneFrame(json.dumps([0] * LARGE_FRAME_BYTES)))


async def test_sliced_history_merges_on_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    source = _source()
    monkeypatch.setattr(HASource, "connected", property(lambda self: True))
    answers = iter(
        [{"light.a": [{"s": "on", "lu": T0}]}, {"light.a": [{"s": "off", "lu": T0 + 1}]}]
        + [{} for _ in range(20)]
    )

    async def _request(self: HASource, *_: Any, **__: Any) -> Any:
        return next(answers)

    monkeypatch.setattr(HASource, "_request", _request)
    threads: list[str] = []
    real_merge = ha_source.merge_history_slices

    def _merge(parts: Any) -> Any:
        threads.append(threading.current_thread().name)
        return real_merge(parts)

    monkeypatch.setattr(ha_source, "merge_history_slices", _merge)
    end = datetime(2026, 1, 3, tzinfo=UTC)
    merged = await source.history(["light.a"], start=end - timedelta(hours=48), end=end)
    assert merged == {"light.a": [{"s": "on", "lu": T0}, {"s": "off", "lu": T0 + 1}]}
    assert threads and threads[0] != threading.main_thread().name
