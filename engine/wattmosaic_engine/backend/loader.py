"""Guarded, lazy construction of the inference backend.

This module deliberately imports nothing from the model/inference layer at module
level.  ``main`` and :mod:`wattmosaic_engine.core.runtime` import it so the engine
stays importable (and startable) when the disaggregation package is absent,
exactly as it did when the factory lived in ``integration.py``.
"""

from __future__ import annotations

from typing import Any


class InferenceBackendUnavailable(RuntimeError):
    """No usable inference backend is installed."""


def load_inference_backend(config: Any, database: Any | None = None) -> Any:
    """Build the inference backend delivered by the orchestrator's adapter.

    This function is the *only* place that touches the backend package and it
    uses a guarded import so the engine always starts.
    ``WattMosaicBackend`` exposes ``process(window, *, device_states, topology,
    models)`` (sync or async) returning an object/dict with ``results``,
    ``reconciliation`` and optional ``diagnostics``.

    The backend is imported lazily so the engine stays importable (and testable)
    when the model layer is absent.
    """
    try:
        from . import build_backend
    except Exception as exc:  # noqa: BLE001 - optional dependency by design
        raise InferenceBackendUnavailable(
            "inference backend not installed (wattmosaic_engine.backend): "
            f"{type(exc).__name__}: {exc}"
        ) from None
    try:
        return build_backend(config, database)
    except Exception as exc:  # noqa: BLE001
        raise InferenceBackendUnavailable(
            f"inference backend failed to initialise: {exc}"
        ) from exc


__all__ = ["InferenceBackendUnavailable", "load_inference_backend"]
