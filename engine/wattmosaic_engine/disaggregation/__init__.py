"""Placement discovery helpers (contract 3.0).

* :mod:`.group_fit` — shared-row co-toggle fitting used by placement discovery
  and by the identifiability analysis;
* :mod:`.auto_placement` — turns discovered placements into proposed v3
  ``MappingEdge`` values.

The legacy absolute solver, the event-isolation / association / circuit /
reconciliation analysis pass and the legacy mapping-matrix projection were
removed at the contract-3.0 cutover; import the submodules directly.
"""

from __future__ import annotations

__all__: list[str] = []
