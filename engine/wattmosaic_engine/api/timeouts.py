"""Request-timeout budgets shared by the engine runtime and the HTTP API.

Why this module exists
----------------------
Placement discovery is the one endpoint whose cost grows with the size of the
window it is asked to analyse, and it is *silent* while it works: the engine
runs a whole-house recorder analysis and writes the response only when the
analysis is finished.  Everything else on the API answers in milliseconds.

``main.py`` already derives the history-query budget from the requested
``lookback_hours``, and the ingress gateway (``addon/gateway.py``) separately
derives its socket-read budget from the largest window the API accepts.  If the
constants lived in ``main.py``, ``api/server.py`` could not import them without
creating a cycle (``main.py`` imports ``APIServer`` from ``server.py``).  They
live here instead, so there is exactly one source of truth and no duplication.

The history constants are measured, not chosen: on the reference install the
whole-house query took 7 s at 24 h and 17 s at 72 h, so ``per_hour=2.5`` gives
168 h a 420 s budget (25x the measured 17 s) while the 60 s floor keeps a short
window from ever waiting longer than a stalled connection would.
"""

from __future__ import annotations

#: The API refuses a window longer than 90 days (``main.py`` rejects
#: ``lookback_hours > 2160``), so 2160 h is the largest history query a caller
#: can legitimately ask for.
MAX_LOOKBACK_HOURS = 2160.0

#: Timeout budget for the placement-discovery history query, as
#: ``max(base, per_hour * lookback_hours)``.  The query cost grows with the
#: window *and* the candidate count.  A timeout there used to surface as "no
#: recorded history", which blamed the recorder for a query that had not
#: finished.
HISTORY_TIMEOUT_BASE_S = 60.0
HISTORY_TIMEOUT_PER_HOUR_S = 2.5

#: Silent time the engine needs *after* the history query: correlation, fitting
#: and placement selection all run before the response is written.  At 24 h the
#: whole request was measured at 9.8 s against a 60 s history floor, so the
#: non-history work was a few seconds; scaling that linearly to 2160 h (90x)
#: gives ~900 s, and 1200 s leaves headroom for the larger fit.  The gateway
#: uses the same allowance when it derives its own socket-read budget.
DISCOVERY_FIT_OVERHEAD_S = 1200.0


def history_timeout_s(lookback_hours: float) -> float:
    """Budget for one placement-discovery history query over ``lookback_hours``."""
    return max(HISTORY_TIMEOUT_BASE_S, HISTORY_TIMEOUT_PER_HOUR_S * lookback_hours)


def discovery_request_timeout_s(lookback_hours: float = MAX_LOOKBACK_HOURS) -> float:
    """Budget for the whole discovery request over the largest allowed window.

    This is *not* the API's default request timeout: it applies only to
    ``POST /api/v1/discover/placements``.  Deriving it from the history budget
    plus the fitting allowance means a 2160 h query gets
    ``2160 * 2.5 + 1200 = 6600 s`` -- the same number the gateway uses for its
    socket-read budget -- and every shorter window gets strictly more than the
    history query alone needs.
    """
    return history_timeout_s(lookback_hours) + DISCOVERY_FIT_OVERHEAD_S


#: Effective middleware budget for the discovery route.  The API's default
#: request timeout stays at 30 s; only this endpoint is defined to be slow.
DISCOVERY_REQUEST_TIMEOUT_S = discovery_request_timeout_s()
