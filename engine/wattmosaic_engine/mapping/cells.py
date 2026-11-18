"""Cell-level honesty helpers for the v3 matrix projection (M1).

These are the reusable rules ported from the legacy
:mod:`wattmosaic_engine.disaggregation.mapping` projection:

* **None, never 0** (HB §18) — :func:`finite_or_none`;
* **byte-identical JSON** — :func:`stable_json`;
* **bounds bracket the estimate** — :func:`validate_bounds_bracket`.

The deterministic visual category (design §8 / HB §19.3) also lives here so it
can be tested in isolation of the full projection.  ``_is_ct_auto_circuit`` from
the legacy module is deliberately **not** ported (``analysis/E-…`` T12).
"""

from __future__ import annotations

import json
import math
from enum import Enum
from typing import Any

from wattmosaic_engine.contracts import (
    Bounds,
    CellVisual,
    EstimateStatus,
    IdentifiabilityStatus,
    MappingStatus,
    Observability,
)

__all__: list[str] = [
    "finite_or_none",
    "stable_json",
    "validate_bounds_bracket",
    "visual_category",
]


def _value(item: Any) -> str:
    """The stored string of a (possibly enum) value.

    Contract models use ``use_enum_values=True``, so a populated cell already
    stores strings; freshly constructed enums need ``.value``.  Both are
    normalised here so the category logic never depends on which form it got.
    """
    if isinstance(item, Enum):
        return str(item.value)
    return str(item)


def finite_or_none(value: Any) -> float | None:
    """Return a finite ``float`` or ``None`` (never ``0.0``, HB §18).

    Booleans are rejected: a ``True`` is not a watt value.  ``NaN`` and the
    infinities are treated as absent, as in the legacy ``_finite`` helper.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _normalise(item: Any) -> Any:
    """Turn cells / models / enums into a JSON-serialisable canonical form."""
    if isinstance(item, Enum):
        return item.value
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    if isinstance(item, dict):
        return {str(key): _normalise(value) for key, value in item.items()}
    if isinstance(item, (list, tuple)):
        return [_normalise(value) for value in item]
    return item


def stable_json(item: Any) -> str:
    """Canonical bytes: two projections of the same inputs are identical.

    Keys are sorted and separators compacted, matching the legacy
    ``MappingMatrix.stable_json``.  Dates serialised by ``model_dump(mode='json')``
    are ISO strings, so a snapshot with the same injected time serialises
    byte-identically across runs.
    """
    return json.dumps(_normalise(item), sort_keys=True, separators=(",", ":"), default=str)


def validate_bounds_bracket(
    estimate: float | None,
    bounds: Bounds | None,
) -> bool:
    """Whether ``bounds`` bracket ``estimate`` (ported honesty rule).

    ``None`` bounds or a ``None`` estimate are vacuously valid.  An absent
    measurement is never replaced by ``0.0`` here.  The strict, raising version
    of this rule already lives on the :class:`MappingCell` contract; this pure
    predicate lets callers check before constructing a cell.
    """
    if bounds is None:
        return True
    low = finite_or_none(bounds.lower_w)
    high = finite_or_none(bounds.upper_w)
    if low is not None and high is not None and low > high:
        return False
    if estimate is not None:
        if low is not None and estimate < low:
            return False
        if high is not None and estimate > high:
            return False
    return True


def visual_category(
    *,
    measurement_status: EstimateStatus | str,
    mapping_status: MappingStatus | str,
    observability: Observability | str,
    group_id: str | None = None,
    identifiability: IdentifiabilityStatus | str | None = None,
) -> CellVisual:
    """The deterministic visual category of one cell (design §8).

    The conditions are evaluated in a fixed order so the result is a pure
    function of the inputs.  Terminal epistemic states (RETIRED, CONFLICTED)
    win first; a direct measurement wins over a provisional mapping; a
    non-identifiable group member is GROUP_ONLY; a NOT_OBSERVABLE mapping is
    never shown as an estimate.
    """
    status = _value(mapping_status)
    measure = _value(measurement_status)
    obs = _value(observability)
    ident = _value(identifiability) if identifiability is not None else None

    if status == MappingStatus.RETIRED.value:
        return CellVisual.RETIRED
    if status == MappingStatus.CONFLICTED.value or obs == Observability.CONFLICTED.value:
        return CellVisual.CONFLICTED
    if measure == EstimateStatus.MEASURED.value:
        return CellVisual.DIRECTLY_MEASURED
    if measure == EstimateStatus.ESTIMATED.value and status == MappingStatus.CONFIRMED.value:
        return CellVisual.CONFIRMED_INFERRED
    if status == MappingStatus.PROVISIONAL.value:
        return CellVisual.PROVISIONAL
    if group_id is not None and ident in (
        IdentifiabilityStatus.NON_IDENTIFIABLE.value,
        IdentifiabilityStatus.GROUP_IDENTIFIABLE.value,
    ):
        return CellVisual.GROUP_ONLY
    if obs == Observability.NOT_OBSERVABLE.value:
        return CellVisual.NOT_OBSERVABLE
    # A status of UNKNOWN with any non-NOT_OBSERVABLE observability, and any
    # other residual combination, falls back to the neutral UNKNOWN category.
    return CellVisual.UNKNOWN
