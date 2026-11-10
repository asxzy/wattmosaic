"""Tests for the Home Assistant ingress panel served by ``addon/gateway.py``.

The panel used to be a single inline HTML string that showed a liveness table.
It is now three static files served under a strict Content-Security-Policy, so
the interesting behaviours are:

* only a Supervisor-ingress peer may see the panel; a direct API client gets a
  JSON descriptor and no assets at all;
* the asset handler serves exactly the allowlisted files, with the right content
  types and the same no-store/CSP headers as the shell;
* an unknown or traversing path returns a structured error instead of a file.

``_peer_trusted`` requires both the Supervisor IP **and** the identity header
that only the ingress proxy adds, so the tests rewrite ``TRUSTED_IPS`` to
``127.0.0.1`` (the test client's peer address) rather than spoofing an IP.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_PATH = REPO_ROOT / "addon" / "gateway.py"
INGRESS_USER = {"X-Remote-User-Id": "test-user"}


def _load_gateway(monkeypatch: pytest.MonkeyPatch):
    """Import addon/gateway.py with a controlled environment.

    The module reads configuration at import time and builds routes from module
    globals, so each test gets a fresh copy.
    """
    monkeypatch.delenv("WM_GATEWAY_TRUSTED_INGRESS_IPS", raising=False)
    monkeypatch.delenv("WM_GATEWAY_UNAUTHENTICATED_NETWORKS", raising=False)
    spec = importlib.util.spec_from_file_location("wm_gateway_panel", GATEWAY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # The test client connects from loopback; trust that peer so the panel path
    # is exercised exactly as the Supervisor proxy would exercise it.
    module.TRUSTED_IPS = {"127.0.0.1"}
    return module


async def _client(gateway):
    """Start the real application without its upstream session startup hook."""
    slash = gateway.create_app()
    client = TestClient(TestServer(slash))
    await client.start_server()
    return client


@pytest.fixture
async def trusted(monkeypatch: pytest.MonkeyPatch):
    gateway = _load_gateway(monkeypatch)
    client = await _client(gateway)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def untrusted(monkeypatch: pytest.MonkeyPatch):
    gateway = _load_gateway(monkeypatch)
    gateway.TRUSTED_IPS = {"192.0.2.5"}
    client = await _client(gateway)
    try:
        yield client
    finally:
        await client.close()


# --------------------------------------------------------------------------- #
# The shell
# --------------------------------------------------------------------------- #


async def test_panel_shell_is_served_to_ingress_peer(trusted: TestClient) -> None:
    response = await trusted.get("/", headers=INGRESS_USER)
    assert response.status == 200
    assert response.content_type == "text/html"
    body = await response.text()
    assert "<title>WattMosaic</title>" in body
    # The shell is a container, not a hardcoded liveness table.
    assert 'id="view"' in body
    # Every asset it names is same-origin and relative, so the same shell works
    # under /api/hassio_ingress/<token>/.
    assert 'src="ui/app.js"' in body
    assert 'href="ui/app.css"' in body


async def test_panel_shell_carries_the_strict_csp(trusted: TestClient) -> None:
    response = await trusted.get("/", headers=INGRESS_USER)
    csp = response.headers["Content-Security-Policy"]
    # No inline script may run; the panel's own file is the only source.
    assert "script-src 'self'" in csp
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    # The ingress URL contains a per-install token; a cached old release must
    # never survive an upgrade.
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_panel_shell_is_json_for_a_direct_peer(untrusted: TestClient) -> None:
    response = await untrusted.get("/", headers=INGRESS_USER)
    assert response.status == 200
    assert response.content_type == "application/json"
    payload = await response.json()
    assert payload["service"] == "wattmosaic-engine"
    assert payload["api"] == "/api/v1"


async def test_panel_shell_requires_the_ingress_identity_header(trusted: TestClient) -> None:
    """Trusting the IP alone would let any sibling container read the panel."""
    response = await trusted.get("/")
    assert response.content_type == "application/json"
    assert 'id="view"' not in await response.text()


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "content_type", "needle"),
    [
        ("/ui/app.css", "text/css", "--accent:"),
        ("/ui/app.js", "text/javascript", "function render()"),
        ("/ui/mark.svg", "image/svg+xml", "<svg"),
    ],
)
async def test_panel_assets_are_served_with_type(
    trusted: TestClient, path: str, content_type: str, needle: str
) -> None:
    response = await trusted.get(path, headers=INGRESS_USER)
    assert response.status == 200
    assert response.content_type == content_type
    assert response.headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert response.headers["Cache-Control"] == "no-store"
    assert needle in await response.text()


async def test_panel_asset_is_denied_to_a_direct_peer(untrusted: TestClient) -> None:
    response = await untrusted.get("/ui/app.js", headers=INGRESS_USER)
    assert response.status == 401
    payload = await response.json()
    assert payload["error"] == "unauthorized"


async def test_unknown_panel_asset_is_a_structured_404(trusted: TestClient) -> None:
    response = await trusted.get("/ui/secrets.txt", headers=INGRESS_USER)
    assert response.status == 404
    payload = await response.json()
    assert payload["error"] == "not_found"


async def test_asset_allowlist_cannot_walk_out_of_the_ui_directory(trusted: TestClient) -> None:
    """A crafted asset name must never return a file outside ``addon/ui``.

    ``aiohttp`` normalises ``..`` before routing, so some of these land on the
    proxy instead of the asset handler.  Whatever the route, the response must
    not be the source of ``gateway.py`` and must not be a 200 file read.
    """
    for path in ("/ui/..%2Fgateway.py", "/ui/%2e%2e/gateway.py", "/ui/gateway.py"):
        response = await trusted.get(path, headers=INGRESS_USER)
        assert response.status != 200, path
        body = await response.text()
        assert "def create_app" not in body, path
        if response.content_type == "application/json":
            # Either the asset handler rejects it (not_found) or the normalised
            # path falls through to the proxy, which cannot read files at all.
            assert (await response.json())["error"] in (
                "not_found",
                "unauthorized",
                "bad_gateway",
            )


async def test_missing_ui_directory_fails_loudly(trusted: TestClient) -> None:
    """A packaged image without the UI must explain itself, not 500."""
    gateway = sys.modules["wm_gateway_panel"]
    original = gateway.UI_INDEX
    gateway.UI_INDEX = Path("/nonexistent/index.html")
    try:
        response = await trusted.get("/", headers=INGRESS_USER)
    finally:
        gateway.UI_INDEX = original
    assert response.status == 503
    payload = await response.json()
    assert payload["error"] == "panel_unavailable"


# --------------------------------------------------------------------------- #
# The static checker itself
# --------------------------------------------------------------------------- #


def _panel_fixture(tmp_path: Path) -> Path:
    """Copy the shipped panel into a temp root the checker can mutate."""
    import shutil

    source = REPO_ROOT / "addon" / "ui"
    target = tmp_path / "ui"
    shutil.copytree(source, target)
    return target


def _panel_checker():
    spec = importlib.util.spec_from_file_location(
        "wm_check_panel", REPO_ROOT / "tools" / "dev" / "check_panel.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_checker_accepts_the_shipped_panel(tmp_path: Path) -> None:
    checker = _panel_checker()
    assert checker.check(_panel_fixture(tmp_path)) == []


def test_checker_rejects_an_external_origin(tmp_path: Path) -> None:
    checker = _panel_checker()
    root = _panel_fixture(tmp_path)
    shell = root / "index.html"
    shell.write_text(
        shell.read_text().replace(
            '<link rel="stylesheet" href="ui/app.css">',
            '<link rel="stylesheet" href="https://cdn.example/app.css">',
        )
    )
    errors = checker.check(root)
    assert any("external URL" in error or "missing asset" in error for error in errors)


def test_checker_rejects_an_inline_script(tmp_path: Path) -> None:
    checker = _panel_checker()
    root = _panel_fixture(tmp_path)
    shell = root / "index.html"
    shell.write_text(
        shell.read_text().replace("<body>", "<body>\n<script>alert(1)</script>")
    )
    assert any("inline <script>" in error for error in checker.check(root))


def test_checker_rejects_an_inline_style_block(tmp_path: Path) -> None:
    checker = _panel_checker()
    root = _panel_fixture(tmp_path)
    shell = root / "index.html"
    shell.write_text(
        shell.read_text().replace("<body>", "<body>\n<style>body{}</style>")
    )
    assert any("inline <style>" in error for error in checker.check(root))


def test_checker_rejects_an_inline_event_handler(tmp_path: Path) -> None:
    checker = _panel_checker()
    root = _panel_fixture(tmp_path)
    shell = root / "index.html"
    shell.write_text(
        shell.read_text().replace("<body>", '<body onload="boot()">')
    )
    assert any("inline event handler" in error for error in checker.check(root))


def test_checker_rejects_a_missing_asset(tmp_path: Path) -> None:
    checker = _panel_checker()
    root = _panel_fixture(tmp_path)
    (root / "app.js").unlink()
    errors = checker.check(root)
    assert any("missing addon/ui/app.js" in error for error in errors)


def test_checker_rejects_eval_and_document_write(tmp_path: Path) -> None:
    checker = _panel_checker()
    root = _panel_fixture(tmp_path)
    script = root / "app.js"
    script.write_text(script.read_text() + "\neval('1'); document.write('x');\n")
    errors = checker.check(root)
    assert any("eval()" in error for error in errors)
    assert any("document.write()" in error for error in errors)
