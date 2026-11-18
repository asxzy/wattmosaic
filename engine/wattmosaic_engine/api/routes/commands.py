"""Command dispatch and the guarded destructive database reset."""

from __future__ import annotations

import asyncio
import math
from typing import Any, Literal

from aiohttp import web
from pydantic import BaseModel, ConfigDict

from ...contracts import ServiceCommand, ServiceResult
from ..facade import FacadeUnavailable, InvalidRequest
from ..server import (
    RESET_DB_ROUTE,
    SLOW_COMMANDS,
    _discovery_jobs,
    _facade,
    _ok,
    _read_json,
    _validate,
    maybe_await,
)


class ResetDatabaseRequest(BaseModel):
    """Typed confirmation for the destructive ``POST /commands/reset-db``.

    ``confirm`` is a required literal, so a bare/unconfirmed call fails pydantic
    validation (HTTP 400) *before* anything is wiped.  ``extra='forbid'`` rejects
    stray keys rather than silently accepting a misspelled confirmation.
    """

    model_config = ConfigDict(extra="forbid")
    confirm: Literal["RESET"]


async def commands(request: web.Request) -> web.Response:
    payload = _validate(ServiceCommand, await _read_json(request))
    if payload.command in SLOW_COMMANDS:
        # The HA integration's ``discover_placements`` service reaches discovery
        # through POST /commands.  Since discovery is now asynchronous, return a
        # job handle here too instead of blocking for the whole analysis.  The
        # handle lives in ``ServiceResult.data`` so the response keeps the
        # command envelope; poll ``GET /discover/placements/{job_id}`` for the
        # result.  This is a deliberate compatibility change: callers that used
        # to receive the full result inline must now poll, but the result shape
        # at the status endpoint is byte-for-byte the old synchronous payload.
        params = payload.parameters
        lookback_raw = params.get("lookback_hours", 24.0)
        if isinstance(lookback_raw, bool) or not isinstance(lookback_raw, (int, float)):
            raise InvalidRequest("lookback_hours must be a number")
        lookback_hours = float(lookback_raw)
        if not math.isfinite(lookback_hours) or lookback_hours <= 0 or lookback_hours > 2160:
            raise InvalidRequest("lookback_hours must be greater than 0 and at most 2160")
        include_groups = params.get("include_groups", True)
        if not isinstance(include_groups, bool):
            raise InvalidRequest("include_groups must be a boolean")
        max_candidates = params.get("max_candidates", 200)
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
            raise InvalidRequest("max_candidates must be an integer")
        if max_candidates < 1:
            raise InvalidRequest("max_candidates must be at least 1")
        apply_raw = params.get("apply")
        if apply_raw is not None and not isinstance(apply_raw, bool):
            raise InvalidRequest("apply must be a boolean")
        handle = await _discovery_jobs(request).start(
            lookback_hours=lookback_hours,
            apply=apply_raw,
            include_groups=include_groups,
            max_candidates=max_candidates,
        )
        result = ServiceResult(
            ok=True,
            command=payload.command,
            detail="discovery started; poll GET /discover/placements/{job_id}",
            data=handle,
        )
        return _ok(result)
    result = await maybe_await(_facade(request).command(payload))
    return _ok(result)


async def reset_database(request: web.Request) -> web.Response:
    """Destructive full reset, guarded by an explicit ``{"confirm": "RESET"}``.

    Auth is inherited from the shared ``_contract_and_auth`` middleware, exactly
    like ``POST /commands`` -- this route does not weaken or bypass it.  The
    typed model is validated first, so an unconfirmed/bare call returns HTTP 400
    and never reaches the (destructive) facade method.  The blocking SQLite work
    runs in a worker thread so it cannot stall ``/health``.
    """
    _validate(ResetDatabaseRequest, await _read_json(request))
    facade = _facade(request)
    method = getattr(facade, "reset_database", None)
    if method is None:
        # Backwards-compatible for facades that predate the optional method:
        # an engine facade still exposes the backing ``.db`` with ``.reset()``.
        db = getattr(facade, "db", None)
        db_reset = getattr(db, "reset", None)
        if db_reset is None:
            raise FacadeUnavailable("database reset is not supported by this backend")
        result: Any = await asyncio.to_thread(db_reset)
    else:
        result = await maybe_await(await asyncio.to_thread(method))
    return _ok(result)


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_post(f"{base}/commands", commands)
    router.add_post(f"{base}{RESET_DB_ROUTE}", reset_database)


__all__ = ["ResetDatabaseRequest", "commands", "register", "reset_database"]
