"""Area protocols that compose into :class:`~wattmosaic_engine.api.facade.EngineFacade`.

The HTTP layer's dependency on the engine is expressed as a set of small,
per-area Protocols instead of one monolithic interface.  Each protocol mirrors
one group of route modules (``api/routes/<area>.py``), so a future task can
replace or extend a single area without redefining what every other area must
provide.  :class:`EngineFacade` simply composes them; it adds no methods of its
own, so existing facades and test doubles keep working unchanged.

Every method returns ``Any`` on purpose: a facade may return a contract model,
a plain JSON-safe dict, or a coroutine resolving to either.  The transport
awaits through ``maybe_await`` and serialises centrally, exactly as before.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..contracts import ServiceCommand


@runtime_checkable
class SnapshotProvider(Protocol):
    """Latest-state reads: health, status and the current device view."""

    def health(self) -> Any: ...

    def status(self) -> Any: ...

    def snapshot(self) -> Any: ...

    def devices(self) -> Any: ...

    def device(self, device_id: str) -> Any: ...

    def confidence_tiers(self) -> Any: ...


@runtime_checkable
class TopologyProvider(Protocol):
    """Topology reads and the declared-topology write path."""

    def topology(self) -> Any: ...

    def set_topology(self, topology: Any) -> Any: ...

    def confirm_topology(self) -> Any: ...

    def panel_tree(self, *, include_nesting: bool = False) -> Any: ...

    def nesting_candidates(self, *, hours: float = 12.0) -> Any: ...

    def circuits(self) -> Any: ...

    def relations(self, kind: str | None = None) -> Any: ...

    def isolation_status(self) -> Any: ...


@runtime_checkable
class MappingProvider(Protocol):
    """The derived mapping matrix and the audit artifacts beside it."""

    def mapping_matrix(self) -> Any: ...

    def inference_records(self, device_id: str, *, limit: int = 200) -> Any: ...

    def residual_components(self, *, hours: float = 24.0, limit: int = 500) -> Any: ...


@runtime_checkable
class ModelsProvider(Protocol):
    """Power-model reads for one device."""

    def models(self, device_id: str) -> Any: ...


@runtime_checkable
class DiscoveryProvider(Protocol):
    """Whole-house placement discovery (a bounded background job)."""

    def discover_placements(
        self,
        *,
        lookback_hours: float = 24.0,
        apply: bool | None = None,
        include_groups: bool = True,
        max_candidates: int = 200,
    ) -> Any: ...


@runtime_checkable
class DiagnosticsProvider(Protocol):
    """Quality, diagnostics, commands, export, metrics and reset."""

    def quality(self) -> Any: ...

    def diagnostics(self) -> Any: ...

    def command(self, command: ServiceCommand) -> Any: ...

    def export(self, device_id: str) -> Any: ...

    def metrics(self) -> Any: ...

    def reset_database(self) -> Any: ...


__all__ = [
    "DiagnosticsProvider",
    "DiscoveryProvider",
    "MappingProvider",
    "ModelsProvider",
    "SnapshotProvider",
    "TopologyProvider",
]
