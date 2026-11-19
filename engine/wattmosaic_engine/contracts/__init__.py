"""Authoritative WattMosaic data contracts (contract 3.0, single source of truth).

This module is a *package facade*: every submodule declares its public names in
``__all__`` and is star-imported here, so callers write
``from wattmosaic_engine.contracts import X``.

* :mod:`.foundation` -- the ``Model`` base, ``utcnow``, ``CONTRACT_VERSION``,
  version ids, evidence refs, status enums, bounds and the confidence vector;
* :mod:`.entities` -- measurement endpoints, physical devices, HA entities;
* :mod:`.measurement` -- readings, HA events, detected events, data quality;
* :mod:`.topology` -- topology/mapping edges and versions, configuration events;
* :mod:`.power_models` -- versioned power-model specs and learning observations;
* :mod:`.inference` -- estimates, joint results and the inference snapshot;
* :mod:`.matrix` -- the measurement-matrix payload;
* :mod:`.service` -- the HTTP health/error envelopes and service commands.
"""

from __future__ import annotations

from .entities import *  # noqa: F403
from .foundation import *  # noqa: F403
from .inference import *  # noqa: F403
from .matrix import *  # noqa: F403
from .measurement import *  # noqa: F403
from .power_models import *  # noqa: F403
from .service import *  # noqa: F403
from .topology import *  # noqa: F403
