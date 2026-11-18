"""Inference package (contract 3.0).

Wave-3/4 inference modules live here.  ``I1`` owns candidate generation, ``I2``
the evidence terms, ``I3`` the joint solver (``joint.py``, ``covariance.py``),
``I4`` identifiability and ``I5`` estimate assembly.  This ``__init__`` stays a
plain package marker so that each task's module can be imported on its own
without pulling in its neighbours.
"""

from __future__ import annotations

__all__: list[str] = []
