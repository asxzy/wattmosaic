"""Whole-house placement discovery routes (bounded background job)."""

from __future__ import annotations

import math
from typing import Any

from aiohttp import web

from ..facade import InvalidRequest, NotFound
from ..server import _discovery_jobs, _ok, _read_json


async def discover_placements(request: web.Request) -> web.Response:
    """Start learning placements from history as a background job (HTTP 202).

    Every field is optional, so an empty body is a valid "show me what you would
    place" request.  Validation here is transport-level only: the bounds match
    ``topology_nesting``'s ``hours`` because both name a lookback over stored
    history, and a non-finite or non-positive value cannot describe a window.

    The response is a job handle (``{job_id, status}``), returned immediately.
    The real work runs in a background task owned by :class:`DiscoveryJobManager`.
    Poll ``GET /discover/placements/{job_id}`` for the status and final result.
    One job runs at a time; a second start while one is active returns the
    existing handle (idempotent).
    """
    body: Any = {}
    if request.can_read_body:
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise InvalidRequest("request body must be a JSON object")

    lookback_raw = body.get("lookback_hours", 24.0)
    if isinstance(lookback_raw, bool) or not isinstance(lookback_raw, (int, float)):
        raise InvalidRequest("lookback_hours must be a number")
    lookback_hours = float(lookback_raw)
    if not math.isfinite(lookback_hours) or lookback_hours <= 0 or lookback_hours > 2160:
        raise InvalidRequest("lookback_hours must be greater than 0 and at most 2160")

    apply_raw = body.get("apply")
    if apply_raw is not None and not isinstance(apply_raw, bool):
        raise InvalidRequest("apply must be a boolean")
    include_groups = body.get("include_groups", True)
    if not isinstance(include_groups, bool):
        raise InvalidRequest("include_groups must be a boolean")
    max_candidates_raw = body.get("max_candidates", 200)
    if isinstance(max_candidates_raw, bool) or not isinstance(max_candidates_raw, int):
        raise InvalidRequest("max_candidates must be an integer")
    if max_candidates_raw < 1:
        raise InvalidRequest("max_candidates must be at least 1")

    # Start the whole-house analysis as a bounded background job and return its
    # handle immediately.  The analysis itself is unchanged -- it just now runs
    # behind a job handle that the caller polls.
    handle = await _discovery_jobs(request).start(
        lookback_hours=lookback_hours,
        apply=apply_raw,
        include_groups=include_groups,
        max_candidates=max_candidates_raw,
    )
    return _ok(handle, status=202)


async def discovery_job_status(request: web.Request) -> web.Response:
    """Return the state (and, when done, the result/error) of a discovery job.

    ``running`` answers with ``{job_id, status}``; ``complete`` adds ``result``
    carrying the same payload the synchronous route used to return; ``failed``
    adds ``error`` with the failure detail.  An unknown/evicted id is a 404.
    """
    job_id = request.match_info["job_id"]
    job = _discovery_jobs(request).status(job_id)
    if job is None:
        raise NotFound(f"discovery job {job_id!r} not found")
    return _ok(job)


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    router.add_post(f"{base}/discover/placements", discover_placements)
    router.add_get(f"{base}/discover/placements/{{job_id}}", discovery_job_status)


__all__ = ["discover_placements", "discovery_job_status", "register"]
