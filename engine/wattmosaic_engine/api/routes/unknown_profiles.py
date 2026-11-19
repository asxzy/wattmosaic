"""T6: the v3 Unknown Load Profile HTTP surface.

``GET /unknown-profiles`` lists persistent profiles (optionally filtered by
``?scope=``), and ``POST /unknown-profiles/{id}/promote`` asks the user-driven
promotion flow to turn one CHARACTERISED/CANDIDATE profile into a Physical
Device with ``origin=unknown_profile_promotion`` and a PROVISIONAL mapping edge.

The route is pure transport: it delegates to the facade's optional
``unknown_profiles`` / ``promote_unknown_profile`` methods, and never imports
:mod:`wattmosaic_engine.runtime.fakes`.  A backend that has not implemented the
surface answers 503 through :class:`FacadeUnavailable`, matching the other v3
route modules.  Registration is unconditional so the surface always exists.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from ..facade import FacadeUnavailable, InvalidRequest
from ..server import _facade, _ok, maybe_await

__all__ = ["list_unknown_profiles", "promote_unknown_profile", "register"]


def _require(facade: Any, name: str) -> Any:
    method = getattr(facade, name, None)
    if method is None:
        raise FacadeUnavailable(f"{name} is not supported by this backend")
    return method


async def list_unknown_profiles(request: web.Request) -> web.Response:
    """GET /unknown-profiles — list profiles, optionally filtered by scope."""
    method = _require(_facade(request), "unknown_profiles")
    scope = request.query.get("scope") or request.query.get("reader_id")
    payload = await maybe_await(method(scope) if scope is not None else method())
    return _ok(payload)


async def promote_unknown_profile(request: web.Request) -> web.Response:
    """POST /unknown-profiles/{id}/promote — promote one profile to a device."""
    profile_id = request.match_info["profile_id"]
    if not profile_id:
        raise InvalidRequest("profile_id is required")
    method = _require(_facade(request), "promote_unknown_profile")
    payload = await maybe_await(method(profile_id))
    return _ok(payload, status=201)


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    """Mount both routes unconditionally (never guarded on ``deps``)."""
    router.add_get(f"{base}/unknown-profiles", list_unknown_profiles)
    router.add_post(
        f"{base}/unknown-profiles/{{profile_id}}/promote", promote_unknown_profile
    )
