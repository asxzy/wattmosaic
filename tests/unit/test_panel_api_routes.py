"""Every API request the panel makes must hit a route the engine serves.

Regression guard: the 3.0 cutover removed ``GET /topology``,
``/topology/nesting``, ``POST /topology/confirm``, ``/mapping``,
``/confidence_tiers`` and ``/isolation`` from the engine, but the panel
(``addon/ui/*.js``) kept requesting them.  Every caller swallowed the 404, so
the Topology tab silently rendered "No topology configured" and nobody noticed.

This test statically extracts every request the panel issues through its HTTP
helpers (``get``/``post``/``api`` in ``core.js``, and the injectable ``http``
parameter whose default is ``post``) and asserts each one matches a route that
``api.server.create_app`` registers, with the same method.  Template-literal
segments (``${encodeURIComponent(id)}``) match a parameterised route segment;
module-level path constants (``const X_PATH = '/...'``) are resolved.  A call
whose path cannot be resolved fails the test, so a new call pattern cannot slip
past the guard unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from wattmosaic_engine.api.server import API_PREFIX, create_app

UI_DIR = Path(__file__).resolve().parents[2] / "addon" / "ui"

#: The panel's HTTP helpers and the method each one issues.  ``http`` is the
#: injectable writer used by the user-action helpers; its default is ``post``
#: (checked below), so it is a POST.
HELPER_METHODS = {"get": "GET", "post": "POST", "http": "POST"}

#: ``get(`` / ``post(`` / ``api(`` / ``http(`` as a bare call (not ``map.get(``).
_CALL = re.compile(r"(?<![\w.$])(get|post|api|http)\(\s*")
_CONST = re.compile(r"^const\s+([A-Z][A-Z0-9_]*)\s*=\s*(['\"`])(/[^'\"`]*)\2\s*;", re.MULTILINE)
_PARAM = "{param}"


def _strip_comments(text: str) -> str:
    """Blank out comments, keeping every newline so reported lines stay true."""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)
    return re.sub(r"(?m)^[ \t]*//.*$", "", text)


def _read_argument(text: str, start: int) -> str:
    """The raw first argument at ``start`` (a literal, template or identifier)."""
    quote = text[start]
    if quote in "'\"`":
        i = start + 1
        depth = 0
        while i < len(text):
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if quote == "`" and text.startswith("${", i):
                depth += 1
                i += 2
                continue
            if quote == "`" and ch == "}" and depth:
                depth -= 1
            elif ch == quote and not depth:
                return text[start : i + 1]
            i += 1
        raise AssertionError(f"unterminated literal at {start}")
    match = re.match(r"[A-Za-z_$][\w$]*", text[start:])
    return match.group(0) if match else text[start : start + 20]


def _resolve(arg: str, constants: dict[str, str]) -> str | None:
    """The request path with ``{param}`` for dynamic segments, or ``None``."""
    if arg in constants:
        path = constants[arg]
    elif arg[:1] in "'\"" and arg[-1:] == arg[:1]:
        path = arg[1:-1]
    elif arg[:1] == "`" and arg[-1:] == "`":
        body = arg[1:-1]

        def sub(match: re.Match[str]) -> str:
            name = match.group(1).strip()
            return constants.get(name, _PARAM)

        path = re.sub(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", sub, body)
    else:
        return None
    if not path.startswith("/"):
        return None
    return path.split("?", 1)[0]


def panel_requests() -> list[tuple[str, str, str]]:
    """``(method, path, where)`` for every API request in ``addon/ui/*.js``."""
    sources = {p.name: _strip_comments(p.read_text(encoding="utf-8")) for p in sorted(UI_DIR.glob("*.js"))}
    constants: dict[str, str] = {}
    for text in sources.values():
        for name, _, value in _CONST.findall(text):
            constants[name] = value
    found: list[tuple[str, str, str]] = []
    unresolved: list[str] = []
    for name, text in sources.items():
        for match in _CALL.finditer(text):
            helper = match.group(1)
            line = text.count("\n", 0, match.start()) + 1
            where = f"{name}:{line}"
            arg = _read_argument(text, match.end())
            # The helper definitions in core.js pass their own ``path`` through.
            if name == "core.js" and arg == "path":
                continue
            if helper == "api":
                unresolved.append(f"{where}: direct api() call; use get()/post() so the guard sees the method")
                continue
            path = _resolve(arg, constants)
            if path is None:
                unresolved.append(f"{where}: {helper}({arg}) — path not statically resolvable")
                continue
            found.append((HELPER_METHODS[helper], path, where))
    assert not unresolved, "panel requests the guard cannot check:\n" + "\n".join(unresolved)
    return found


def engine_routes() -> set[tuple[str, str]]:
    """``(method, path)`` for every route ``create_app`` registers, minus the prefix."""
    routes: set[tuple[str, str]] = set()
    for route in create_app().router.routes():
        if route.method == "HEAD" or route.resource is None:
            continue
        canonical = route.resource.canonical
        if canonical.startswith(API_PREFIX):
            routes.add((route.method, canonical[len(API_PREFIX):]))
    return routes


def _segments_match(panel: str, route: str) -> bool:
    p_parts = panel.strip("/").split("/")
    r_parts = route.strip("/").split("/")
    if len(p_parts) != len(r_parts):
        return False
    for p, r in zip(p_parts, r_parts, strict=True):
        route_param = r.startswith("{") and r.endswith("}")
        if p == _PARAM:
            if not route_param:
                return False
        elif not route_param and p != r:
            return False
    return True


def route_for(method: str, path: str, routes: set[tuple[str, str]]) -> str | None:
    # Prefer an exact literal route, then a parameterised one.
    if (method, path) in routes:
        return path
    for route_method, route in sorted(routes):
        if route_method == method and _segments_match(path, route):
            return route
    return None


def test_extraction_sees_the_panel_requests() -> None:
    """The extractor itself works: it finds the known reads and writes."""
    requests = {(m, p) for m, p, _ in panel_requests()}
    for expected in (
        ("GET", "/health"),
        ("GET", "/topology/v3"),
        ("GET", "/topology/tree"),
        ("GET", "/endpoints"),
        ("GET", "/matrix"),
        ("GET", "/devices/{param}"),
        ("GET", "/discover/placements/{param}"),
        ("POST", "/discover/placements"),
        ("POST", "/topology/edges/{param}/pin"),
        ("POST", "/topology/edges/{param}/reject"),
        ("POST", "/endpoints/{param}/scope"),
    ):
        assert expected in requests, f"extractor missed {expected}"


def test_http_parameter_defaults_to_post() -> None:
    """``http`` is mapped to POST; that is only true while its default is ``post``."""
    for path in sorted(UI_DIR.glob("*.js")):
        text = path.read_text(encoding="utf-8")
        for signature in re.findall(r"function\s+\w+\(([^)]*\bhttp\b[^)]*)\)", text):
            assert re.search(r"\bhttp\s*=\s*post\b", signature), (path.name, signature)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/topology"),
        ("GET", "/topology/nesting"),
        ("POST", "/topology/confirm"),
        ("GET", "/mapping"),
        ("GET", "/confidence_tiers"),
        ("GET", "/isolation"),
    ],
)
def test_removed_routes_are_not_served(method: str, path: str) -> None:
    """The routes the panel used to call really are gone (so the guard bites)."""
    assert route_for(method, path, engine_routes()) is None


def test_every_panel_request_matches_an_engine_route() -> None:
    routes = engine_routes()
    missing = [
        f"{method} {path}  ({where})"
        for method, path, where in panel_requests()
        if route_for(method, path, routes) is None
    ]
    assert not missing, (
        "the panel requests routes the engine does not serve:\n  " + "\n  ".join(missing)
    )
