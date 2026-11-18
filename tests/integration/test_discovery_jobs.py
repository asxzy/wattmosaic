"""Integration: placement discovery runs as a bounded background job.

The owner directive is that discovery is "nothing but an event reply used for
initialization" and must not hold an HTTP request open for minutes (the cause of
gateway 504s).  These tests run a real aiohttp server with a controllable facade
and prove the end-to-end job lifecycle over a real socket:

* POST ``/discover/placements`` answers 202 with a job handle *before* the slow
  discovery coroutine is allowed to finish (an ``asyncio.Event`` gate proves the
  POST does not await the work);
* GET ``/discover/placements/{job_id}`` reports ``running`` then ``complete``
  with the exact result payload the synchronous path used to return;
* a failing job reports ``error`` without crashing the server;
* a second start while one is running returns the existing handle (idempotent,
  one job at a time) and finished-job history is bounded;
* ``POST /commands`` with ``discover_placements`` also returns a handle;
* shutdown cancels an in-flight job without leaving a dangling task.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from wattmosaic_engine.api.facade import NullFacade
from wattmosaic_engine.api.server import DISCOVERY_JOB_HISTORY_LIMIT, APIServer


class _GatedFacade(NullFacade):
    """A facade whose discovery blocks on an event until the test releases it."""

    def __init__(self) -> None:
        super().__init__("gated")
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.calls: list[dict[str, Any]] = []
        self.failure: Exception | None = None

    async def discover_placements(
        self,
        *,
        lookback_hours: float = 24.0,
        apply: bool | None = None,
        include_groups: bool = True,
        max_candidates: int = 200,
    ) -> Any:
        self.calls.append(
            {
                "lookback_hours": lookback_hours,
                "apply": apply,
                "include_groups": include_groups,
                "max_candidates": max_candidates,
            }
        )
        self.started.set()
        await self.gate.wait()
        if self.failure is not None:
            raise self.failure
        return {
            "ok": True,
            "applied": False,
            "lookback_hours": lookback_hours,
            "topology_version": 3,
            "stats": {},
            "accepted": [],
            "refused": {},
            "groups": [],
            "withheld": {},
            "skipped": {},
            "notes": [],
        }


async def _serve(
    facade: _GatedFacade,
    fn: Any,
    **server_kwargs: Any,
) -> Any:
    server = APIServer(facade, host="127.0.0.1", port=0, api_token="", **server_kwargs)
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            return await fn(session, f"http://127.0.0.1:{server.port}/api/v1", server)
    finally:
        # Release any job left waiting so stop() never hangs on a gated coroutine.
        facade.gate.set()
        await server.stop()


def test_post_returns_before_discovery_completes() -> None:
    """The POST must not await the slow analysis; the status endpoint polls it."""

    async def fn(session: aiohttp.ClientSession, base: str, server: APIServer) -> Any:
        async with session.post(f"{base}/discover/placements", json={}) as resp:
            assert resp.status == 202, await resp.text()
            handle = await resp.json()
        assert handle["status"] == "running"
        job_id = handle["job_id"]

        # The background task has started but is blocked on the gate.  Even so,
        # the POST above already returned -- proving it did not await the work.
        facade.started.clear()
        async with session.get(f"{base}/discover/placements/{job_id}") as resp:
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body == {"job_id": job_id, "status": "running"}

        # Release the analysis; the next poll shows the frozen result shape.
        facade.gate.set()
        for _ in range(100):
            async with session.get(f"{base}/discover/placements/{job_id}") as resp:
                body = await resp.json()
            if body["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        assert body["status"] == "complete"
        assert body["job_id"] == job_id
        result = body["result"]
        assert result["ok"] is True
        assert result["groups"] == []
        assert result["accepted"] == []
        assert "status" not in result  # result is the unchanged sync payload
        return True

    facade = _GatedFacade()
    assert asyncio.run(_serve(facade, fn))


def test_second_start_is_idempotent_and_only_one_job_runs() -> None:
    """While a job is active, another start returns the same handle (one job)."""

    async def fn(session: aiohttp.ClientSession, base: str, server: APIServer) -> Any:
        async with session.post(f"{base}/discover/placements", json={}) as resp:
            first = await resp.json()
        async with session.post(
            f"{base}/discover/placements", json={"lookback_hours": 72}
        ) as resp:
            second = await resp.json()
        assert first["job_id"] == second["job_id"]
        assert second["status"] == "running"

        facade.gate.set()
        for _ in range(100):
            async with session.get(f"{base}/discover/placements/{first['job_id']}") as resp:
                body = await resp.json()
            if body["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        # The winning job used the *first* request's parameters; the second start
        # did not schedule a competing analysis.
        assert facade.calls == [
            {
                "lookback_hours": 24.0,
                "apply": None,
                "include_groups": True,
                "max_candidates": 200,
            }
        ]
        return True

    facade = _GatedFacade()
    assert asyncio.run(_serve(facade, fn))


def test_failed_job_reports_error_without_crashing() -> None:
    """A discovery exception is captured as a failed job, not a 500."""

    async def fn(session: aiohttp.ClientSession, base: str, server: APIServer) -> Any:
        async with session.post(f"{base}/discover/placements", json={}) as resp:
            handle = await resp.json()
        facade.gate.set()
        for _ in range(100):
            async with session.get(f"{base}/discover/placements/{handle['job_id']}") as resp:
                assert resp.status == 200, await resp.text()
                body = await resp.json()
            if body["status"] == "failed":
                break
            await asyncio.sleep(0.01)
        assert body["status"] == "failed"
        assert body["error"] == "boom"
        assert "result" not in body
        # The server is still healthy after a failed background job.
        async with session.get(f"{base}/health") as resp:
            assert resp.status == 200
        return True

    facade = _GatedFacade()
    facade.failure = RuntimeError("boom")
    assert asyncio.run(_serve(facade, fn))


def test_unknown_job_id_is_404() -> None:
    async def fn(session: aiohttp.ClientSession, base: str, server: APIServer) -> Any:
        async with session.get(f"{base}/discover/placements/does-not-exist") as resp:
            assert resp.status == 404, await resp.text()
            body = await resp.json()
        assert body["error"] == "not_found"
        return True

    assert asyncio.run(_serve(_GatedFacade(), fn))


def test_commands_path_also_returns_a_job_handle() -> None:
    """The HA integration's /commands service gets the same async contract."""

    async def fn(session: aiohttp.ClientSession, base: str, server: APIServer) -> Any:
        async with session.post(
            f"{base}/commands",
            json={"command": "discover_placements", "parameters": {"lookback_hours": 72}},
        ) as resp:
            assert resp.status == 200, await resp.text()
            envelope = await resp.json()
        assert envelope["ok"] is True
        assert envelope["command"] == "discover_placements"
        handle = envelope["data"]
        assert handle["status"] == "running"
        job_id = handle["job_id"]

        facade.gate.set()
        for _ in range(100):
            async with session.get(f"{base}/discover/placements/{job_id}") as resp:
                body = await resp.json()
            if body["status"] == "complete":
                break
            await asyncio.sleep(0.01)
        assert body["result"]["ok"] is True
        assert facade.calls[0]["lookback_hours"] == 72.0
        return True

    facade = _GatedFacade()
    assert asyncio.run(_serve(facade, fn))


def test_shutdown_cancels_in_flight_job_without_dangling_task() -> None:
    """APIServer.stop() cancels a running discovery task cleanly."""

    async def fn(session: aiohttp.ClientSession, base: str, server: APIServer) -> Any:
        async with session.post(f"{base}/discover/placements", json={}) as resp:
            handle = await resp.json()
        old_tasks = [t for t in asyncio.all_tasks() if "wm-discovery" in (t.get_name() or "")]
        assert len(old_tasks) == 1
        facade.gate.set()
        await server.stop()
        # After stop the discovery task is gone (cancelled and awaited).
        remaining = [t for t in asyncio.all_tasks() if "wm-discovery" in (t.get_name() or "")]
        assert remaining == []
        return handle["job_id"]

    facade = _GatedFacade()
    # _serve's finally calls stop() again; stop() is idempotent, so this is safe.
    assert asyncio.run(_serve(facade, fn))


def test_finished_job_history_is_bounded() -> None:
    """Repeated completed jobs do not grow the store past the cap."""

    async def fn(session: aiohttp.ClientSession, base: str, server: APIServer) -> Any:
        from wattmosaic_engine.api.server import DISCOVERY_JOBS_KEY

        manager = server.app[DISCOVERY_JOBS_KEY]
        # Run more jobs than the cap by completing between each start.
        total = DISCOVERY_JOB_HISTORY_LIMIT + 5
        for _ in range(total):
            facade.gate.set()
            async with session.post(f"{base}/discover/placements", json={}) as resp:
                handle = await resp.json()
            for _ in range(100):
                async with session.get(
                    f"{base}/discover/placements/{handle['job_id']}"
                ) as resp:
                    body = await resp.json()
                if body["status"] == "complete":
                    break
                await asyncio.sleep(0.005)
        # Private store is bounded; the oldest ids are evicted (404 on poll).
        assert len(manager._jobs) == DISCOVERY_JOB_HISTORY_LIMIT
        return True

    facade = _GatedFacade()
    assert asyncio.run(_serve(facade, fn))
