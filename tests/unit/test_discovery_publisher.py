"""Tests for ``addon/discovery.py`` — the App-install discovery publisher.

The publisher is the piece that removes the step where a user has to guess a
container hostname and port.  It runs inside the App, talks to the Supervisor,
and must never prevent the App from starting.

Two properties matter and are both asserted here:

* the announced message contains exactly what the integration needs and nothing
  secret;
* every failure mode (no Supervisor, refused endpoint, slow engine) is
  non-fatal, because the App has to boot on a plain Docker host too.

The HTTP interactions are stubbed.  No network is used and no real credentials
are involved.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DISCOVERY_PATH = REPO_ROOT / "addon" / "discovery.py"


@pytest.fixture()
def publisher(monkeypatch: pytest.MonkeyPatch):
    """Import ``addon/discovery.py`` fresh with a controlled environment."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token-not-real")
    monkeypatch.setenv("WM_SUPERVISOR_URL", "http://supervisor")
    monkeypatch.delenv("WM_DISCOVERY_HOST", raising=False)
    monkeypatch.delenv("WM_DISCOVERY_PORT", raising=False)
    monkeypatch.delenv("WM_DISCOVERY_SERVICE", raising=False)
    monkeypatch.delenv("WM_DISCOVERY_ENABLED", raising=False)
    monkeypatch.delenv("WM_DISCOVERY_WAIT_S", raising=False)
    spec = importlib.util.spec_from_file_location("wm_discovery_under_test", DISCOVERY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]):
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    name = "wm_discovery_" + str(abs(hash(tuple(sorted(env.items())))))
    spec = importlib.util.spec_from_file_location(name, DISCOVERY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_flow_logic():
    """Import the integration's ``flow_logic`` without importing Home Assistant."""
    package_dir = REPO_ROOT / "custom_components" / "wattmosaic"
    name = "wm_discovery_flow_logic"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, package_dir / "__init__.py", submodule_search_locations=[str(package_dir)]
        )
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        # Do NOT exec the spec: __init__ imports homeassistant.
        module.__path__ = [str(package_dir)]  # type: ignore[attr-defined]
        sys.modules[name] = module
    return importlib.import_module(f"{name}.flow_logic")


# --------------------------------------------------------------------------- #
# The announced message
# --------------------------------------------------------------------------- #

class TestMessage:
    def test_service_is_the_integration_domain(self, publisher) -> None:
        """The Supervisor routes the message to the flow named by the service."""
        assert publisher._service() == "wattmosaic"

    def test_message_carries_host_and_port_only(self, publisher, monkeypatch) -> None:
        monkeypatch.setenv("WM_DISCOVERY_HOST", "local-wattmosaic-engine")
        message = publisher.discovery_message()
        assert message == {
            "service": "wattmosaic",
            "config": {"host": "local-wattmosaic-engine", "port": "8787"},
        }

    def test_message_contains_no_secret(self, publisher, monkeypatch) -> None:
        """The Supervisor stores and forwards the config; it must stay boring."""
        monkeypatch.setenv("WM_DISCOVERY_HOST", "local-wattmosaic-engine")
        monkeypatch.setenv("WM_API_TOKEN", "super-secret-api-token")
        monkeypatch.setenv("WM_HA_TOKEN", "super-secret-ha-token")
        serialised = json.dumps(publisher.discovery_message())
        assert "secret" not in serialised
        assert "token" not in serialised.lower()

    def test_default_port_matches_the_gateway(self, publisher) -> None:
        assert publisher._port() == "8787"

    def test_service_name_is_overridable(self, monkeypatch) -> None:
        module = _load(
            monkeypatch,
            {
                "SUPERVISOR_TOKEN": "t",
                "WM_DISCOVERY_SERVICE": "wattmosaic_beta",
                "WM_DISCOVERY_HOST": "engine",
            },
        )
        assert module.discovery_message()["service"] == "wattmosaic_beta"

    def test_no_host_means_no_useful_message(self, publisher, monkeypatch) -> None:
        monkeypatch.setenv("WM_DISCOVERY_HOST", "")
        monkeypatch.setattr(publisher, "_supervisor_self_info", lambda: {})
        monkeypatch.setattr(publisher.socket, "gethostname", lambda: "localhost")
        monkeypatch.setattr(publisher.os, "environ", {})
        message = publisher.discovery_message()
        assert "host" not in message["config"]


class TestHostResolution:
    """The App's DNS name is not fixed, so it is resolved in a defined order."""

    def test_override_wins(self, publisher, monkeypatch) -> None:
        monkeypatch.setenv("WM_DISCOVERY_HOST", "explicit.example")
        monkeypatch.setattr(publisher, "_supervisor_self_info", lambda: {"hostname": "sup"})
        assert publisher.announced_host() == "explicit.example"

    def test_supervisor_record_wins_over_the_container_hostname(
        self, publisher, monkeypatch
    ) -> None:
        """A locally installed App is hashed; the Supervisor is authoritative."""
        monkeypatch.setattr(
            publisher, "_supervisor_self_info", lambda: {"hostname": "f48de52e-wattmosaic-engine"}
        )
        monkeypatch.setattr(publisher.socket, "gethostname", lambda: "wattmosaic-engine")
        assert publisher.announced_host() == "f48de52e-wattmosaic-engine"

    def test_container_hostname_is_used_when_the_record_is_unavailable(
        self, publisher, monkeypatch
    ) -> None:
        monkeypatch.setattr(publisher, "_supervisor_self_info", lambda: {})
        monkeypatch.setattr(
            publisher.socket, "gethostname", lambda: "f48de52e-wattmosaic-engine"
        )
        assert publisher.announced_host() == "f48de52e-wattmosaic-engine"

    @pytest.mark.parametrize(
        "hostname",
        [
            "localhost",
            "LOCALHOST",
            "localhost.localdomain",
            "ip6-localhost",
            "127.0.0.1",
            "0.0.0.0",
            "::1",
            "[::1]",
            "",
        ],
    )
    def test_loopback_hostnames_are_never_announced(
        self, publisher, monkeypatch, hostname: str
    ) -> None:
        """Announcing "localhost" would point Home Assistant at itself."""
        monkeypatch.setattr(publisher, "_supervisor_self_info", lambda: {})
        monkeypatch.setattr(publisher.socket, "gethostname", lambda: hostname)
        monkeypatch.setenv("WM_APP_SLUG", "local_wattmosaic_engine")
        assert publisher.announced_host() == "local-wattmosaic-engine"

    @pytest.mark.parametrize(
        "override",
        [
            "localhost",
            "127.0.0.1",
            "user@evil.example",
            "engine.local/path",
            "http://engine.local",
            "engine.local:9000",
            "engine.local?x=1",
            "not a host",
        ],
    )
    def test_an_override_the_integration_would_reject_is_not_announced(
        self, publisher, monkeypatch, override: str
    ) -> None:
        """Falling through to the next candidate beats shipping a broken card.

        A discovery message that cannot be turned into an engine URL is worse
        than no message: the user gets a card that only ever fails.
        """
        monkeypatch.setenv("WM_DISCOVERY_HOST", override)
        monkeypatch.setattr(
            publisher, "_supervisor_self_info", lambda: {"hostname": "app-host"}
        )
        assert publisher.announced_host() == "app-host"

    def test_slug_rule_is_the_last_resort(self, publisher, monkeypatch) -> None:
        monkeypatch.setattr(publisher, "_supervisor_self_info", lambda: {})
        monkeypatch.setattr(publisher.socket, "gethostname", lambda: "localhost")
        monkeypatch.setenv("WM_APP_SLUG", "deadbeef_wattmosaic_engine")
        assert publisher.announced_host() == "deadbeef-wattmosaic-engine"

    @pytest.mark.parametrize(
        "host",
        [
            "f48de52e-wattmosaic-engine",
            "local-wattmosaic-engine",
            "engine.local",
            "engine.local.",
            "app_1.example.com",
            "deadbeef",
            "[fd00::1]",
        ],
    )
    def test_the_integration_accepts_every_host_this_module_announces(
        self, publisher, host: str
    ) -> None:
        """The two sides must agree on what a usable host is.

        ``addon/discovery.py`` is not importable as part of the integration
        package, so this asserts the shared rule directly: any host that passes
        ``is_announceable_host`` must produce a URL from the config flow, and the
        reverse.  Drift here is invisible in production — one side silently
        stops producing (or accepting) discovery cards.
        """
        flow_logic = _load_flow_logic()
        assert publisher.is_announceable_host(host), host
        assert flow_logic.discovered_base_url({"host": host, "port": 8787}) == (
            f"http://{host}:8787"
        )

    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "127.0.0.1",
            "user@evil.example",
            "engine.local/path",
            "http://engine.local",
            "engine.local:9000",
            "engine.local?x=1",
            "not a host",
            "-engine",
            "engine..local",
        ],
    )
    def test_the_two_sides_reject_the_same_hosts(self, publisher, host: str) -> None:
        flow_logic = _load_flow_logic()
        assert not publisher.is_announceable_host(host), host
        assert flow_logic.discovered_base_url({"host": host, "port": 8787}) is None


class TestSupervisorRead:
    def test_missing_token_makes_no_request(self, publisher, monkeypatch) -> None:
        monkeypatch.setattr(publisher, "TOKEN", "")
        called = []
        monkeypatch.setattr(
            publisher.urllib.request, "urlopen", lambda *a, **k: called.append(a)
        )
        assert publisher._supervisor_self_info() == {}
        assert called == []

    @pytest.mark.parametrize(
        "error",
        [
            OSError("boom"),
            ValueError("not json"),
        ],
    )
    def test_any_failure_yields_an_empty_record(self, publisher, error) -> None:
        def boom(*_args, **_kwargs):
            raise error

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(publisher.urllib.request, "urlopen", boom)
            assert publisher._supervisor_self_info() == {}


class TestPublishOutcome:
    def test_success(self, publisher, monkeypatch) -> None:
        class Response:
            status = 200

            def read(self):
                return b'{"result": "ok"}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(publisher.urllib.request, "urlopen", lambda *a, **k: Response())
        assert publisher.publish({"service": "wattmosaic", "config": {"host": "h", "port": "8787"}})

    def test_refusal_is_reported_not_raised(self, publisher, monkeypatch) -> None:
        import urllib.error

        def refuse(*_args, **_kwargs):
            raise urllib.error.HTTPError(
                "http://supervisor/discovery", 403, "Forbidden", {}, None
            )

        monkeypatch.setattr(publisher.urllib.request, "urlopen", refuse)
        assert publisher.publish({"service": "wattmosaic", "config": {}}) is False

    def test_unreachable_is_reported_not_raised(self, publisher, monkeypatch) -> None:
        import urllib.error

        def unreachable(*_args, **_kwargs):
            raise urllib.error.URLError("no route")

        monkeypatch.setattr(publisher.urllib.request, "urlopen", unreachable)
        assert publisher.publish({"service": "wattmosaic", "config": {}}) is False

    def test_request_carries_the_supervisor_token_header(self, publisher, monkeypatch) -> None:
        seen: dict[str, Any] = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def capture(request, **_kwargs):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            seen["method"] = request.get_method()
            seen["body"] = request.data
            return Response()

        monkeypatch.setattr(publisher.urllib.request, "urlopen", capture)
        assert publisher.publish({"service": "wattmosaic", "config": {"host": "h"}})
        assert seen["url"] == "http://supervisor/discovery"
        assert seen["auth"] == "Bearer test-token-not-real"
        assert seen["method"] == "POST"
        assert json.loads(seen["body"])["service"] == "wattmosaic"


# --------------------------------------------------------------------------- #
# main() — never fatal
# --------------------------------------------------------------------------- #


class TestMain:
    def test_disabled_does_nothing(self, monkeypatch, capsys) -> None:
        module = _load(
            monkeypatch,
            {"SUPERVISOR_TOKEN": "t", "WM_DISCOVERY_ENABLED": "false"},
        )
        published = []
        monkeypatch.setattr(module, "publish", lambda m: published.append(m) or True)
        assert module.main([]) == 0
        assert published == []

    def test_outside_supervisor_exits_zero_without_publishing(
        self, monkeypatch, capsys
    ) -> None:
        """Plain Docker / CI / a laptop: there is nothing to announce."""
        module = _load(monkeypatch, {"SUPERVISOR_TOKEN": None, "WM_DISCOVERY_HOST": "engine"})
        published = []
        monkeypatch.setattr(module, "publish", lambda m: published.append(m) or True)
        assert module.main([]) == 0
        assert published == []
        out = capsys.readouterr().out
        assert "skipping" in out
        assert "test-token" not in out

    def test_dry_run_prints_the_message_and_publishes_nothing(
        self, monkeypatch, capsys
    ) -> None:
        module = _load(
            monkeypatch,
            {
                "SUPERVISOR_TOKEN": "test-token-not-real",
                "WM_DISCOVERY_HOST": "local-wattmosaic-engine",
                "WM_DISCOVERY_ENABLED": "true",
            },
        )
        published = []
        monkeypatch.setattr(module, "publish", lambda m: published.append(m) or True)
        assert module.main(["--dry-run"]) == 0
        assert published == []
        printed = json.loads(capsys.readouterr().out)
        assert printed["service"] == "wattmosaic"
        assert printed["config"]["host"] == "local-wattmosaic-engine"
        assert "test-token-not-real" not in json.dumps(printed)

    def test_publishes_after_the_gateway_answers(self, monkeypatch) -> None:
        module = _load(
            monkeypatch,
            {
                "SUPERVISOR_TOKEN": "t",
                "WM_DISCOVERY_HOST": "engine",
                "WM_DISCOVERY_WAIT_S": "30",
            },
        )
        monkeypatch.setattr(module, "_gateway_ready", lambda: True)
        published = []
        monkeypatch.setattr(module, "publish", lambda m: published.append(m) or True)
        assert module.main([]) == 0
        assert published and published[0]["service"] == "wattmosaic"

    def test_still_boots_when_the_gateway_never_answers(self, monkeypatch) -> None:
        """A dead engine must not turn into a dead App."""
        module = _load(
            monkeypatch,
            {
                "SUPERVISOR_TOKEN": "t",
                "WM_DISCOVERY_HOST": "engine",
                "WM_DISCOVERY_WAIT_S": "0",
            },
        )
        monkeypatch.setattr(module, "_gateway_ready", lambda: False)
        published = []
        monkeypatch.setattr(module, "publish", lambda m: published.append(m) or True)
        assert module.main([]) == 0
        # It publishes anyway: the integration probes before creating an entry,
        # so a stale announcement is harmless.
        assert published

    def test_gives_up_quietly_when_every_attempt_fails(
        self, monkeypatch, capsys
    ) -> None:
        module = _load(
            monkeypatch,
            {
                "SUPERVISOR_TOKEN": "t",
                "WM_DISCOVERY_HOST": "engine",
                "WM_DISCOVERY_WAIT_S": "0",
            },
        )
        monkeypatch.setattr(module, "_gateway_ready", lambda: True)
        attempts: list[dict[str, Any]] = []

        def failing(message: dict[str, Any]) -> bool:
            attempts.append(message)
            return False

        monkeypatch.setattr(module, "publish", failing)
        monkeypatch.setattr(module.time, "sleep", lambda s: None)
        assert module.main([]) == 0
        # It really did try, and it really did give up.
        assert len(attempts) == 6  # the first attempt plus five retries
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
        messages = [line["msg"] for line in lines]
        assert any("could not be published" in m for m in messages), messages
        # The bearer token must never reach the log, at any severity.
        for line in lines:
            assert "test-token-not-real" not in json.dumps(line)

    def test_no_host_means_no_publish(self, monkeypatch) -> None:
        module = _load(
            monkeypatch,
            {"SUPERVISOR_TOKEN": "t", "WM_DISCOVERY_HOST": ""},
        )
        monkeypatch.setattr(module, "_supervisor_self_info", lambda: {})
        monkeypatch.setattr(module.socket, "gethostname", lambda: "localhost")
        monkeypatch.setattr(module.os, "environ", {})
        published = []
        monkeypatch.setattr(module, "publish", lambda m: published.append(m) or True)
        assert module.main([]) == 0
        assert published == []
