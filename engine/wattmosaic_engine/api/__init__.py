"""The HTTP API and the facade that decouples it from the inference engine.

The API never imports ``electrical``, ``disaggregation``, ``learning`` or
``models``.  It talks to whatever object satisfies :class:`EngineFacade`, which
``main.EngineCore`` implements.  This keeps the transport layer testable on its
own and lets the inference backend be swapped without touching HTTP code.
"""

from .facade import EngineFacade, FacadeUnavailable, NullFacade, StubFacade, maybe_await
from .server import APIServer, create_app, create_runner

__all__ = [
    "APIServer",
    "EngineFacade",
    "FacadeUnavailable",
    "NullFacade",
    "StubFacade",
    "create_app",
    "create_runner",
    "maybe_await",
]
