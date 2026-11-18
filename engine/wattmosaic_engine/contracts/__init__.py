"""Authoritative WattMosaic data contracts (single source of truth).

This module is a *package facade*.  The original single-file module now lives
in :mod:`wattmosaic_engine.contracts.legacy` and is re-exported verbatim so
that the ~66 callers doing ``from wattmosaic_engine.contracts import X`` keep
working with no behaviour change.  The seven submodules below are owned by the
Wave-1 contract tasks; each starts empty (``__all__ = []``) and is star-imported
here.  This facade is written once and must never need editing again: as those
tasks fill their submodules, their exports appear here automatically.
"""

from __future__ import annotations

# 1. Legacy surface, verbatim.  ``legacy`` defines no ``__all__`` so the star
#    import brings across every public name (models, enums, helpers, and the
#    imported typing/pydantic names that callers have come to rely on).
from .entities import *  # noqa: F403
from .foundation import *  # noqa: F403
from .inference import *  # noqa: F403
from .legacy import *  # noqa: F403
from .legacy import __annotations__ as _legacy_annotations
from .legacy import _document as _document  # noqa: F401
from .legacy import _reject_parent_cycle as _reject_parent_cycle  # noqa: F401
from .matrix import *  # noqa: F403
from .measurement import *  # noqa: F403
from .power_models import *  # noqa: F403
from .topology import *  # noqa: F403

# Preserve the legacy module's ``__annotations__`` (``TIER_RANK``) so reflective
# callers see the same mapping they did before the split.  A plain ``globals``
# assignment avoids registering this name as an annotation itself.
globals()["__annotations__"] = dict(_legacy_annotations)
del _legacy_annotations
