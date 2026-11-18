"""Inference package (contract 3.0): candidates, matching, joint solve, groups.

Wave-3/4 tasks own the modules in here:

* I1 ``candidates`` / ``topology_filter``
* I2 ``temporal`` / ``evidence``
* I3 ``joint`` / ``covariance``
* I4 ``identifiability`` / ``bounds``
* I5 ``estimates`` / ``confidence_v3``
* I6 ``matching`` / ``records``
* I7 ``orchestrator``

This facade re-exports only the modules that exist.  It deliberately imports
nothing eagerly so a partially landed wave stays importable; use
``from wattmosaic_engine.inference.identifiability import ...``.
"""

from __future__ import annotations

__all__: list[str] = []
