"""Matrix core: projection (M1) and the store/updater/reconciler (M2).

A pure, deterministic projection from the typed runtime handoffs (``K08``) and
the v3 matrix contracts (``K05``) into the sparse cell set, the L0–L4 column
tree, the DEC-27 row tree and the two aggregation summaries.

Everything here is a pure function of its inputs: there is no database access
and no wall clock.  A clock/time is always taken from an injected argument (the
snapshot's ``computed_at`` / ``observed_at``), which is what makes replay
byte-identical (AT-19).  Absent measurements are ``None``, never ``0`` (HB §18).

This package deliberately imports only the runtime *Protocols* from
``wattmosaic_engine.runtime.interfaces``.  It never imports ``runtime.fakes``.
"""

from __future__ import annotations

from .cells import (
    finite_or_none,
    stable_json,
    validate_bounds_bracket,
    visual_category,
)
from .columns import build_columns
from .projection import (
    materialise_cells,
    materialise_matrix,
    physical_total_from_cells,
    reader_totals_from_cells,
)
from .reconcile import Reconciler
from .rows import build_rows
from .store import CellEventsV3, MatrixStore
from .updater import MatrixUpdater

__all__: list[str] = [
    "finite_or_none",
    "stable_json",
    "validate_bounds_bracket",
    "visual_category",
    "build_columns",
    "build_rows",
    "materialise_cells",
    "materialise_matrix",
    "physical_total_from_cells",
    "reader_totals_from_cells",
    "CellEventsV3",
    "MatrixStore",
    "MatrixUpdater",
    "Reconciler",
]
