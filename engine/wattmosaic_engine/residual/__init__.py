"""Per-reader residual engine and learned unknown-load prior (T3).

The public surface lives in :mod:`reader_residual`; this facade keeps the
package importable and gives callers one stable import path::

    from wattmosaic_engine.residual import ReaderResidualEngine

Production code here never imports :mod:`wattmosaic_engine.runtime.fakes`; the
corresponding tests use those deterministic doubles instead.
"""

from __future__ import annotations

from .reader_residual import (
    DEFAULT_PENALTY_SCALE,
    INITIAL_PENALTY_SCALE_FRACTION,
    MAE_WINDOW_DAYS,
    PRIOR_WINDOW_DAYS,
    ROLLING_SCALE_DAYS,
    ReaderResidualEngine,
    ResidualPriorJob,
    UnknownPriorModel,
    assignment_diagnostics,
    estimate_unknown_prior,
)

__all__: list[str] = [
    "DEFAULT_PENALTY_SCALE",
    "INITIAL_PENALTY_SCALE_FRACTION",
    "MAE_WINDOW_DAYS",
    "PRIOR_WINDOW_DAYS",
    "ROLLING_SCALE_DAYS",
    "ReaderResidualEngine",
    "ResidualPriorJob",
    "assignment_diagnostics",
    "UnknownPriorModel",
    "estimate_unknown_prior",
]
