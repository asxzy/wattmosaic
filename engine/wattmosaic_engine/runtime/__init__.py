"""Runtime handoff types for contract 3.0 (K08).

A package facade over :mod:`wattmosaic_engine.runtime.interfaces` so callers may
write ``from wattmosaic_engine.runtime import ObservationOperator``.  The test
doubles in :mod:`wattmosaic_engine.runtime.fakes` are deliberately **not**
re-exported: fakes must never be reachable from production code.
"""

from __future__ import annotations

from .interfaces import *  # noqa: F403
from .interfaces import __all__ as _interfaces_all

__all__: list[str] = list(_interfaces_all)
