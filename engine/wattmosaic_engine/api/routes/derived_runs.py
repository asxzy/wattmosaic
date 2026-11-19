"""L7: the versioned derived-run provenance route.

    GET /provenance/derived_run/{run_id}

A derived run is resolved through D4's :class:`ProvenanceResolver`: the route
returns the nine HB §18 answers for every derived estimate the run produced,
plus the run's version triple and provenance metadata.  The run itself is a
read of the versioned derived namespace; it never writes measurements or
evidence (DEC-35).

Registered unconditionally by :func:`register` (it does not depend on ``deps``).
"""

from __future__ import annotations

from aiohttp import web

from ..facade import FacadeUnavailable
from ..server import _facade, _ok, maybe_await

__all__ = ["register", "derived_run"]


def _derived_run_payload(derived_run_id: str) -> dict[str, object] | None:
    """Resolve a derived run through D4's resolver, or None when unknown."""
    from wattmosaic_engine.learning.reconciliation import resolve_derived_run
    from wattmosaic_engine.replay import get_derived_run

    try:
        run = get_derived_run(derived_run_id)
    except KeyError:
        return None
    answers = resolve_derived_run(run)
    return {
        "derived_run_id": run.derived_run_id,
        "versions": run.versions.model_dump(mode="json"),
        "reason": run.reason,
        "chunks": run.chunks,
        "fingerprint": run.fingerprint,
        "superseded_by": run.superseded_by,
        "created_at": run.created_at.isoformat(),
        "answers": [answer.as_dict() for answer in answers],
    }


async def derived_run(request: web.Request) -> web.Response:
    """Serve ``GET /provenance/derived_run/{run_id}``."""
    # Prefer the facade's provider when one is attached (a live engine may keep
    # its own derived namespace).  Fall back to the module registry, which D4
    # resolves through the same typed answers as a live snapshot.
    payload: object = None
    method = getattr(_facade(request), "derived_run", None)
    if callable(method):
        try:
            payload = await maybe_await(method(request.match_info["run_id"]))
        except FacadeUnavailable:
            payload = None
    if payload is None:
        payload = _derived_run_payload(request.match_info["run_id"])
    return _ok(payload)


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    """Register the derived-run provenance path unconditionally."""
    router.add_get(f"{base}/provenance/derived_run/{{run_id}}", derived_run)
