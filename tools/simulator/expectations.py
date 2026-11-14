"""Expected interpretations for synthetic fixtures.

A fixture is only useful as a test if it states, in the same object, *what the
numbers are* and *what a correct reader should conclude from them*.  This module
holds that second half so every fixture variant returns the same shape.

The three layers are deliberately separate and never collapse:

``observations``
    what the integration is given (values, timestamps, declarations).
``ground_truth``
    the physical numbers a test asserts against.
``expected``
    the interpretation a correct consumer must reach — including the right to
    refuse.  ``explainable=False`` is a *result*, not a failure: the variants in
    the Hot Tub matrix that cannot be explained are exactly the ones that must
    produce ``UNKNOWN``/``CONFLICT`` rather than a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ExpectedInterpretation:
    """What a correct consumer must conclude from a fixture's observations."""

    basis: str
    """Expected ``MeasurementBasis`` value for the metered reading(s)."""

    appliance_total_w: float | None
    """The true appliance total the evidence supports, or ``None`` when the
    evidence does not support any total.  Never invented."""

    single_ct_estimate_w: float | None = None
    """The total a *single* CT's factor metadata asserts, or ``None`` when no
    such claim is made.  Compared against :attr:`appliance_total_w` to show
    where a factor does and does not explain the physics."""

    summing_allowed: bool = True
    """Whether the fixture's CTs may be summed as independent loads.  ``False``
    for the Hot Tub pair: two legs (or two whole-load estimates) of *one*
    appliance are an overlap, not two loads."""

    explainable: bool = True
    """Whether the available metadata can explain the observed step.  ``False``
    means the correct outcome is a refusal (``UNKNOWN``/``CONFLICT``)."""

    conflict: bool = False
    """Whether two independent sources (HA state vs CT evidence) disagree."""

    reason: str = ""
    """One sentence a reader can check against the numbers."""

    details: dict[str, Any] = field(default_factory=dict)
    """Variant-specific extras (leg shares, intervals, per-CT deltas)."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "basis": self.basis,
            "appliance_total_w": self.appliance_total_w,
            "single_ct_estimate_w": self.single_ct_estimate_w,
            "summing_allowed": self.summing_allowed,
            "explainable": self.explainable,
            "conflict": self.conflict,
            "reason": self.reason,
            "details": dict(self.details),
        }


__all__ = ["ExpectedInterpretation"]
