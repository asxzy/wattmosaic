"""Guard the route split: discovery, the route snapshot and the area protocols.

``X04`` moved every handler out of ``api/server.py`` into per-area modules under
``api/routes``.  This test pins two properties that make the split safe:

* :func:`register_all` discovers its modules with ``pkgutil.iter_modules`` and
  every discovered module exposes ``register(router, base, deps)``.  A later task
  can add a module without editing either ``server.py`` or the package facade.
* The exact set of ``(method, path)`` routes registered by ``create_app`` equals
  the snapshot captured from the unmodified tree before the split.  Any drift --
  a dropped route, a renamed path, a changed verb -- fails here rather than
  silently changing the HTTP surface.
"""

from __future__ import annotations

import importlib
import pkgutil

from wattmosaic_engine.api import routes
from wattmosaic_engine.api.server import create_app

# Captured from ``create_app()`` on the unmodified tree (main@27373aa) before X04.
ROUTE_SNAPSHOT: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/circuits"),
        ("GET", "/api/v1/confidence_tiers"),
        ("GET", "/api/v1/devices"),
        ("GET", "/api/v1/devices/{device_id}"),
        ("GET", "/api/v1/devices/{device_id}/records"),
        ("GET", "/api/v1/diagnostics"),
        ("GET", "/api/v1/discover/placements/{job_id}"),
        ("GET", "/api/v1/endpoints"),
        ("GET", "/api/v1/endpoints/{endpoint_id}"),
        ("GET", "/api/v1/events"),
        ("GET", "/api/v1/events/{group_id}"),
        ("GET", "/api/v1/export/{device_id}"),
        ("GET", "/api/v1/groups"),
        ("GET", "/api/v1/groups/{group_id}"),
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/isolation"),
        ("GET", "/api/v1/mapping"),
        ("GET", "/api/v1/matrix"),
        ("GET", "/api/v1/matrix/cells/{row_id}/{reader_id}"),
        ("GET", "/api/v1/matrix/readers/{id}"),
        ("GET", "/api/v1/matrix/rows/{id}"),
        ("GET", "/api/v1/metrics"),
        ("GET", "/api/v1/models/{device_id}"),
        ("GET", "/api/v1/provenance/{kind}/{id}"),
        ("GET", "/api/v1/provenance/derived_run/{run_id}"),
        ("GET", "/api/v1/quality"),
        ("GET", "/api/v1/relations"),
        ("GET", "/api/v1/residuals"),
        ("GET", "/api/v1/snapshot"),
        ("GET", "/api/v1/status"),
        ("GET", "/api/v1/topology"),
        ("GET", "/api/v1/topology/nesting"),
        ("GET", "/api/v1/topology/tree"),
        ("GET", "/api/v1/topology/v3"),
        ("GET", "/api/v1/topology/versions"),
        ("GET", "/api/v1/topology/versions/{version_id}"),
        ("POST", "/api/v1/topology/edges"),
        ("POST", "/api/v1/topology/edges/{edge_id}/pin"),
        ("POST", "/api/v1/topology/edges/{edge_id}/reject"),
        ("PUT", "/api/v1/topology/endpoints/{endpoint_id}/config"),
        ("POST", "/api/v1/commands"),
        ("POST", "/api/v1/commands/reset-db"),
        ("POST", "/api/v1/discover/placements"),
        ("POST", "/api/v1/endpoints/{endpoint_id}/scope"),
        ("POST", "/api/v1/topology/confirm"),
        ("PUT", "/api/v1/endpoints/{endpoint_id}"),
        ("PUT", "/api/v1/topology"),
    }
)

# Every module X04 created.  A new area module may be added later; it must still
# satisfy the ``register(router, base, deps)`` contract.
EXPECTED_MODULES = frozenset(
    {
        "commands",
        "derived_runs",
        "diagnostics",
        "devices",
        "discovery",
        "endpoints",
        "events",
        "export",
        "health",
        "mapping",
        "matrix",
        "metrics",
        "models",
        "snapshot",
        "snapshot_v3",
        "topology",
    }
)


def _registered_routes() -> set[tuple[str, str]]:
    app = create_app()
    return {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if getattr(route, "method", None) and getattr(route, "resource", None)
        # aiohttp registers an implicit HEAD alongside every GET; ignore it so
        # the snapshot names only the routes the app factory declared.
        and route.method != "HEAD"
    }


def test_registered_routes_match_snapshot() -> None:
    assert _registered_routes() == set(ROUTE_SNAPSHOT)


def test_discovery_finds_every_area_module() -> None:
    discovered = {info.name for info in pkgutil.iter_modules(routes.__path__)}
    assert discovered >= EXPECTED_MODULES


def test_every_discovered_module_exposes_register() -> None:
    for info in pkgutil.iter_modules(routes.__path__):
        module = importlib.import_module(f"{routes.__name__}.{info.name}")
        register = getattr(module, "register", None)
        assert callable(register), f"{info.name} has no register(router, base, deps)"
        # ``register`` must accept exactly the three positional args the facade
        # passes, with no new required arguments.
        import inspect

        params = list(inspect.signature(register).parameters.values())
        required = [p for p in params if p.default is inspect.Parameter.empty]
        assert len(required) == 3, f"{info.name}.register has unexpected required params"


def test_register_all_returns_sorted_module_names() -> None:
    from aiohttp import web

    router = web.UrlDispatcher()
    names = routes.register_all(router, "/api/v1", deps=None)
    assert names == sorted(names)
    assert set(names) >= EXPECTED_MODULES
    # Registering into a fresh router produces the same route set as create_app.
    registered = {
        (route.method, route.resource.canonical)
        for route in router.routes()
        if getattr(route, "method", None) and getattr(route, "resource", None)
        and route.method != "HEAD"
    }
    assert registered == set(ROUTE_SNAPSHOT)


def test_facade_composes_area_protocols() -> None:
    from wattmosaic_engine.api.facade import EngineFacade
    from wattmosaic_engine.api.protocols import (
        DiagnosticsProvider,
        DiscoveryProvider,
        MappingProvider,
        ModelsProvider,
        SnapshotProvider,
        TopologyProvider,
    )

    for protocol in (
        SnapshotProvider,
        TopologyProvider,
        MappingProvider,
        ModelsProvider,
        DiscoveryProvider,
        DiagnosticsProvider,
    ):
        assert issubclass(EngineFacade, protocol)
