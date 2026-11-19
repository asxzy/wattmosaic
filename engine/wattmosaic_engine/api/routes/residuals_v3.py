"""T3: the v3 ``GET /residuals?reader_id=`` route.

The legacy ``GET /residuals`` handler (``mapping.residual_components``) is
registered first by auto-discovery (``mapping`` sorts before ``residuals_v3``).
The v3 task owns *this* module only, so ``register`` wraps that resource's GET
handler with a dispatcher: with ``?reader_id=`` the per-reader residual points,
the learned ``UnknownPrior`` and the over/under-assignment diagnostic are
published; without it the legacy component payload is returned unchanged.
Adding a second GET on the same path would be unreachable in aiohttp (and is
asserted against elsewhere in the suite), so the existing handler is wrapped.

The route is pure transport: heavy reads are delegated to the facade and never
import ``runtime.fakes``.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from ..facade import FacadeUnavailable
from ..server import CONFIG_KEY, _facade, _jsonable, _ok, maybe_await

__all__: list[str] = ["register", "residuals_v3"]


def _penalty_scale(request: web.Request) -> float:
    """``WM_UNKNOWN_PENALTY_SCALE`` (DEC-23), default 1.0."""
    from wattmosaic_engine.residual.reader_residual import DEFAULT_PENALTY_SCALE

    raw = request.app.get(CONFIG_KEY, {}).get("wm_unknown_penalty_scale", None)
    if raw is None:
        return DEFAULT_PENALTY_SCALE
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PENALTY_SCALE
    return value if value >= 0.0 else DEFAULT_PENALTY_SCALE


def _default_payload(request: web.Request) -> Any:
    method = getattr(_facade(request), "residual_components", None)
    if method is None:
        raise FacadeUnavailable("residual components are not supported by this backend")
    return method()


def _coerce_point(value: Any) -> Any:
    from wattmosaic_engine.contracts import ReaderResidualPoint

    if isinstance(value, ReaderResidualPoint):
        return value
    if isinstance(value, dict):
        return ReaderResidualPoint.model_validate(value)
    return value


async def residuals_v3(request: web.Request) -> web.Response:
    """Serve ``GET /residuals?reader_id=`` (per-reader points + diagnostic)."""
    reader_id = request.query.get("reader_id")
    if not reader_id:
        return _ok(await maybe_await(_default_payload(request)))

    from wattmosaic_engine.residual.reader_residual import (
        UnknownPriorModel,
        assignment_diagnostics,
    )

    facade = _facade(request)
    points_method = getattr(facade, "reader_residual_points", None)
    if points_method is None:
        raise FacadeUnavailable("per-reader residuals are not supported by this backend")
    raw_points = await maybe_await(points_method(reader_id))
    points = [_coerce_point(p) for p in raw_points]

    prior = None
    model = getattr(facade, "residual_model", None)
    if model is not None and callable(getattr(model, "unknown_prior", None)):
        at_fn = getattr(facade, "now", None)
        if not callable(at_fn):
            from wattmosaic_engine.contracts import utcnow

            at_fn = utcnow
        prior = await maybe_await(model.unknown_prior(reader_id, at_fn()))
    if prior is None:
        prior = UnknownPriorModel(penalty_scale=_penalty_scale(request)).initial_prior(reader_id)

    diagnostics = assignment_diagnostics(points)
    payload = {
        "points": [_jsonable(p) for p in points],
        "unknown_prior": {
            "scope_reader_id": prior.scope_reader_id,
            "mu_w": prior.mu_w,
            "sigma_w": prior.sigma_w,
            "lambda_u": prior.lambda_u,
            "step_rate_per_h": prior.step_rate_per_h,
        },
        "diagnostics": [
            {
                "scope_reader_id": d.scope_reader_id,
                "day": d.day.isoformat(),
                "unexplained_share": d.unexplained_share,
                "subject_share": d.subject_share,
                "residual_share": d.residual_share,
            }
            for d in diagnostics
        ],
    }
    return _ok(payload)


def register(router: web.UrlDispatcher, base: str, deps: object) -> None:
    """Wrap the existing ``/residuals`` GET route with the v3 reader filter."""
    path = f"{base}/residuals"
    for resource in router.resources():
        if getattr(resource, "canonical", None) != path:
            continue
        for route in resource:
            if route.method == "GET" and getattr(route, "_handler", None) is not None:
                legacy = route._handler

                async def dispatch(request: web.Request, _legacy: Any = legacy) -> web.StreamResponse:
                    if request.query.get("reader_id"):
                        return await residuals_v3(request)
                    return await maybe_await(_legacy(request))

                route._handler = dispatch
                return
    # Defensive: no legacy resource yet (a bare router in a test).
    router.add_get(path, residuals_v3)
