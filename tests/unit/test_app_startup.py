"""Regression tests for the Home Assistant App startup path.

Three independent defects made a stock App install fail to start:

* #34 — ``EngineConfig.from_env`` appended ``:8123`` to any URL without a port,
  so the Supervisor Core proxy ``http://supervisor/core`` became
  ``http://supervisor/core:8123``.  ``urlsplit`` reads that as host
  ``supervisor`` and path ``/core:8123``, which the Supervisor answers with
  HTTP 401, so ingestion never connected.
* #35 — with an empty ``api_token`` the gateway denied *every* peer, including
  loopback, so the container HEALTHCHECK could never pass.  The Supervisor then
  timed out waiting for startup and its watchdog restarted the app forever.
* The HEALTHCHECK called the engine API directly, but ``WM_API_TOKEN`` is set by
  ``run.sh`` inside PID 1 and is absent from the probe's environment, so that
  request was always unauthenticated.

Each test here fails against the previous implementation; the docstrings name
the behaviour that was wrong, not just the shape of the fix.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_PATH = REPO_ROOT / "addon" / "gateway.py"

from wattmosaic_engine.config.settings import EngineConfig, _with_default_port  # noqa: E402
from wattmosaic_engine.storage.database import Database  # noqa: E402


def _load_gateway(monkeypatch: pytest.MonkeyPatch, *, api_token: str):
    """Import addon/gateway.py with a controlled environment.

    The module reads its configuration at import time, so it has to be loaded
    fresh for each token value.
    """
    monkeypatch.setenv("WM_API_TOKEN", api_token)
    monkeypatch.delenv("WM_GATEWAY_TRUSTED_INGRESS_IPS", raising=False)
    monkeypatch.delenv("WM_GATEWAY_UNAUTHENTICATED_NETWORKS", raising=False)
    spec = importlib.util.spec_from_file_location(f"wm_gateway_{api_token or 'empty'}", GATEWAY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Request:
    """Minimal stand-in for ``aiohttp.web.Request``."""

    def __init__(self, remote: str, headers: dict[str, str] | None = None) -> None:
        self.remote = remote
        self.headers = headers or {}


# --------------------------------------------------------------------------- #
# #34 — default port must not corrupt a path-carrying URL
# --------------------------------------------------------------------------- #


def test_core_proxy_url_keeps_its_path() -> None:
    """The Supervisor proxy URL must survive URL normalisation unchanged.

    Regression: the old heuristic appended ``:8123`` whenever no colon followed
    ``//``, producing ``http://supervisor/core:8123`` — a different host *and*
    a mangled path.
    """
    cfg = EngineConfig.from_env({"WM_HA_URL": "http://supervisor/core"})
    assert cfg.ha_url == "http://supervisor/core"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Bare host: completing the default port is the intended convenience.
        ("http://homeassistant", "http://homeassistant:8123"),
        ("http://homeassistant.local", "http://homeassistant.local:8123"),
        ("http://192.0.2.10", "http://192.0.2.10:8123"),
        # Explicit port: untouched.
        ("http://homeassistant:8123", "http://homeassistant:8123"),
        ("https://ha.example:8443", "https://ha.example:8443"),
        # Path or reverse-proxy prefix: untouched.
        ("http://supervisor/core", "http://supervisor/core"),
        ("http://supervisor/core/", "http://supervisor/core/"),
        ("https://ha.example/ha", "https://ha.example/ha"),
    ],
)
def test_default_port_only_applies_to_bare_hosts(raw: str, expected: str) -> None:
    assert _with_default_port(raw) == expected


def test_core_proxy_url_survives_full_config_load() -> None:
    """The engine must dial the proxy path the Supervisor actually serves."""
    cfg = EngineConfig.from_env(
        {
            "WM_HA_URL": "http://supervisor/core",
            "WM_HA_TOKEN": "placeholder",
        }
    )
    assert cfg.ha_url == "http://supervisor/core"
    assert cfg.ha_url.endswith("/core")
    assert cfg.validate() == []


def test_default_port_does_not_break_ipv6_or_query() -> None:
    """A bracketed IPv6 literal with a port, or a query, is preserved."""
    assert _with_default_port("http://[fd00::1]:8123") == "http://[fd00::1]:8123"
    assert _with_default_port("http://ha.example?x=1") == "http://ha.example:8123?x=1"


# --------------------------------------------------------------------------- #
# #35 — an empty api_token must not lock out loopback / the Supervisor network
# --------------------------------------------------------------------------- #


def test_empty_token_allows_loopback_healthcheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container HEALTHCHECK connects from loopback and sends no token.

    Regression: ``_authorised`` returned ``(False, False)`` for every peer when
    ``API_TOKEN`` was empty, so the probe received 401 and Docker never reported
    the container healthy.
    """
    gateway = _load_gateway(monkeypatch, api_token="")
    allowed, via_ingress = gateway._authorised(_Request("127.0.0.1"))
    assert allowed is True
    assert via_ingress is False


def test_empty_token_allows_supervisor_network_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Home Assistant Core reaches the app from the Supervisor's internal network."""
    gateway = _load_gateway(monkeypatch, api_token="")
    allowed, _ = gateway._authorised(_Request("172.30.32.1"))
    assert allowed is True


def test_empty_token_denies_a_public_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty token must still fail closed for anything outside the app network.

    This is the negative control for the fix above: relaxing the empty-token
    path must not turn the gateway into an open proxy when the user publishes a
    host port.
    """
    gateway = _load_gateway(monkeypatch, api_token="")
    allowed, _ = gateway._authorised(_Request("198.51.100.7"))
    assert allowed is False


def test_configured_token_still_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a token set, loopback without the header must stay unauthorised."""
    gateway = _load_gateway(monkeypatch, api_token="s3cret")
    allowed, _ = gateway._authorised(_Request("127.0.0.1"))
    assert allowed is False
    allowed, _ = gateway._authorised(_Request("127.0.0.1", {"X-API-Token": "s3cret"}))
    assert allowed is True
    allowed, _ = gateway._authorised(_Request("127.0.0.1", {"X-API-Token": "wrong"}))
    assert allowed is False


def test_ingress_peer_still_trusted_by_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Supervisor ingress peer is trusted by IP *and* identity header only."""
    gateway = _load_gateway(monkeypatch, api_token="")
    trusted = _Request("172.30.32.2", {"X-Remote-User-Id": "abc"})
    assert gateway._authorised(trusted) == (True, True)
    # Same IP without the Supervisor-injected identity header is not ingress.
    spoof = _Request("172.30.32.2")
    assert gateway._authorised(spoof)[1] is False


# --------------------------------------------------------------------------- #
# HEALTHCHECK — the probe must not depend on a token it cannot see
# --------------------------------------------------------------------------- #


def test_healthcheck_never_uses_the_api_token() -> None:
    """The probe must authenticate through the gateway, not with WM_API_TOKEN.

    ``run.sh`` exports ``WM_API_TOKEN`` inside PID 1; a Docker HEALTHCHECK exec
    does not inherit it. The old probe sent an empty token, was rejected with
    401 and reported the container unhealthy on every App install.

    This is a static guard: it fails if a future edit reintroduces a direct,
    token-authenticated call from the probe.
    """
    source = (REPO_ROOT / "addon" / "healthcheck.py").read_text()
    # Only the executable code matters; the module docstring legitimately
    # explains *why* WM_API_TOKEN is unavailable here.
    code = source.split('"""', 2)[2] if source.count('"""') >= 2 else source
    assert "WM_API_TOKEN" not in code, (
        "healthcheck.py must not read WM_API_TOKEN: a Docker HEALTHCHECK does not "
        "inherit PID 1's environment, so the token is always empty there. Probe "
        "/healthz instead, which the gateway authenticates itself."
    )
    assert "/api/v1/health" not in code, (
        "healthcheck.py must not call the engine API directly; route through "
        "/healthz so the token stays in the gateway process."
    )
    assert "/healthz" in code


def test_healthcheck_timing_fits_the_supervisor_window() -> None:
    """A terminal health state must be reachable inside the 120 s startup window.

    The Supervisor waits ``STARTUP_TIMEOUT = 120`` seconds for an app to leave
    STARTUP. With ``start_period + retries * interval`` at 90 + 3*30 = 180 s the
    first terminal result arrived long after the timeout had already fired.
    """
    import re

    text = (REPO_ROOT / "addon" / "Dockerfile").read_text()
    match = re.search(
        r"HEALTHCHECK\s+--interval=(?P<interval>\d+)s\s+--timeout=(?P<timeout>\d+)s"
        r"\s+--start-period=(?P<start>\d+)s\s+--retries=(?P<retries>\d+)",
        text,
    )
    assert match is not None, "addon/Dockerfile must keep a parseable HEALTHCHECK line"
    interval = int(match.group("interval"))
    timeout = int(match.group("timeout"))
    start = int(match.group("start"))
    retries = int(match.group("retries"))
    worst_case = start + retries * (interval + timeout)
    assert worst_case <= 110, (
        f"worst-case time to a terminal health state is {worst_case}s; the "
        "Supervisor gives up waiting after 120s"
    )


# --------------------------------------------------------------------------- #
# The runtime boundary: one process, one SQLite file, no new dependency
# --------------------------------------------------------------------------- #


def test_one_process_one_database(tmp_path: Path) -> None:
    """REQ-FOUNDATION-004: the target adds no dependency, DB, service or bus.

    The engine is one process with one writer.  ``EngineConfig`` resolves a
    single ``database_path`` (env ``WM_DATABASE_PATH``, default
    ``data/wattmosaic.db``); ``Database`` opens that path once for writing and
    a short-lived read-only ``_reader`` on **the same file**, never a second
    database.  ``PRAGMA database_list`` proves both connections point at one
    file, and ``query_only`` proves the reader cannot write.
    """
    db_path = tmp_path / "wattmosaic.db"
    cfg = EngineConfig.from_env(
        {"WM_DATABASE_PATH": str(db_path), "WM_HA_TOKEN": "not-used"},
        dotenv_path=tmp_path / "absent.env",
    )
    assert cfg.database_path == str(db_path)
    # The default resolves to the one conventional file, relative to the repo.
    assert EngineConfig().database_path == "data/wattmosaic.db"

    db = Database(cfg.database_path)
    try:
        # The writer's own connection.
        writer_files = {
            Path(row[2]).resolve()
            for row in db._conn.execute("PRAGMA database_list").fetchall()
            if row[2]
        }
        assert writer_files == {db_path.resolve()}

        # The short-lived reader: same file, and read-only by enforcement.
        with db._reader() as reader:
            reader_files = {
                Path(row[2]).resolve()
                for row in reader.execute("PRAGMA database_list").fetchall()
                if row[2]
            }
            assert reader_files == {db_path.resolve()}
            assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                reader.execute("CREATE TABLE writer_must_not_be_able_to_write (id INTEGER)")
    finally:
        db.close()

    # No second database file: the directory holds the one file (plus SQLite's
    # WAL sidecars for that same file, never a different name).
    assert db_path.exists()
    names = {p.name for p in tmp_path.iterdir()}
    assert names <= {db_path.name, f"{db_path.name}-wal", f"{db_path.name}-shm"}


def test_no_new_runtime_dependency() -> None:
    """``[project].dependencies`` is the explicit six; nothing else ships.

    Read from the repository file, so a dependency added anywhere else (a new
    import, an optional extra promoted to runtime) fails here.  The list is
    literal on purpose: a rename, a reorder or an addition is a visible diff.
    """
    pyproject = REPO_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == [
        "pydantic>=2.6,<3",
        "aiohttp>=3.9,<4",
        "websockets>=12,<16",
        "numpy>=1.26,<3",
        "scipy>=1.11,<2",
        "PyYAML>=6,<7",
    ]
