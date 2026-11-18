"""Per-area HTTP route modules, discovered at app-factory time.

Each module in this package exposes ``register(router, base, deps)`` and owns a
contiguous group of endpoints.  :func:`register_all` discovers the modules with
``pkgutil.iter_modules`` (sorted, so route order is deterministic), so a later
task can add or remove a route module without editing ``api/server.py`` or this
file.  This is a Wave-0 package facade: it is written once and must not need
edits in later waves.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from aiohttp import web


def register_all(router: web.UrlDispatcher, base: str, deps: Any) -> list[str]:
    """Discover every route module, register it, and return the module names.

    ``deps`` is an opaque dependency bundle passed through to each module's
    ``register``; today the modules take what they need from the aiohttp app via
    the typed keys in ``api.deps``.  Returning the discovered names makes the
    discovery testable without starting a server.
    """
    names = sorted(info.name for info in pkgutil.iter_modules(__path__))
    for name in names:
        module = importlib.import_module(f"{__name__}.{name}")
        module.register(router, base, deps)
    return names


__all__ = ["register_all"]
