"""Electrical topology and the observation operator (contract 3.0).

* :mod:`.topology_graph` — the lifecycle-filtered graph over
  :class:`~wattmosaic_engine.contracts.TopologyEdgeV3` edges (T1);
* :mod:`.operator` — ``H(T, M, t)``, the observation operator (T2);
* :mod:`.factors` — per-reader measurement factors and basis classification;
* :mod:`.topology_inference` — the topology evidence ledger (T5);
* :mod:`.topology_change` — measurement-inferred configuration changes (T7);
* :mod:`.solver` / :mod:`.nesting_math` / :mod:`.window` — numeric helpers.

The pointer-based legacy topology graph, the measurement-matrix solver and the
hierarchy finder were removed at the contract-3.0 cutover; import the
submodules directly.
"""

from __future__ import annotations

__all__: list[str] = []
