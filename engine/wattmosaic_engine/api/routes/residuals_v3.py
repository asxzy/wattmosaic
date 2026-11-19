"""T3: the v3 ``GET /residuals`` route.

Without ``?reader_id=`` the facade's signed residual-component history is
returned.  With ``?reader_id=`` the per-reader residual points, the learned
``UnknownPrior`` and the over/under-assignment diagnostic are published.

The route is pure transport: heavy reads are delegated to the facade and never
import ``runtime.fakes``.  It is mounted unconditionally.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from ..facade import FacadeUnavailable
from ..server import CONFIG_KEY, _bounded_float, _facade, _jsonable, _ok, maybe_await

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


def _coerce_point(value: Any) -> Any:
    from wattmosaic_engine.contracts import ReaderResidualPoint

    if isinstance(value, ReaderResidualPoint):
        return value
    if isinstance(value, dict):
        return ReaderResidualPoint.model_validate(value)
    return value


async def _component_history(request: web.Request) -> Any:
    """The signed per-component residual history (no reader filter)."""
    hours = _bounded_float(request, "hours", default=24.0, minimum=0.0, maximum=2160.0)
    limit_raw = _bounded_float(request, "limit", default=500.0, minimum=1.0, maximum=5000.0)
    method = getattr(_facade(request), "residual_components", None)
    if method is None:
        raise FacadeUnavailable("residual components are not supported by this backend")
    payload = await asyncio.to_thread(method, hours=hours, limit=int(limit_raw))
    return await maybe_await(payload)


async def residuals_v3(request: web.Request) -> web.Response:
    """Serve ``GET /residuals`` and ``GET /residuals?reader_id=``."""
    reader_id = request.query.get("reader_id")
    if not reader_id:
        return _ok(await _component_history(request))

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
    """Mount ``GET /residuals`` unconditionally."""
    router.add_get(f"{base}/residuals", residuals_v3)
