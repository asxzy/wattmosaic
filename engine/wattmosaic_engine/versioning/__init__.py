"""Version lifecycle service (D1, rule 20, HB §14, interfaces.md §7).

One service owns the independent topology and mapping lifecycles: the current
and as-of :class:`~wattmosaic_engine.contracts.VersionTriple`, configuration-event
recording, SUSPECTED_CHANGE marking and append-only propose/promote/rollback.
The model-set id comes from L1's registry and is injected here; this service
only assembles the triple and never couples the three lifecycles.

The module is pure: all time comes from an injected ``Clock`` and all state from
the injected S14/S15 repositories.  ``runtime.fakes`` is never imported.
"""

from .service import (
    VersionConflictError,
    VersionService,
)

__all__ = [
    "VersionConflictError",
    "VersionService",
]
