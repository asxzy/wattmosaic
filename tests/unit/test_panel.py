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
import re
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


# --------------------------------------------------------------------------- #
# Discover-card copy: no raw engine code reaches a person
# --------------------------------------------------------------------------- #


def _app_js() -> str:
    return (REPO_ROOT / "addon" / "ui" / "app.js").read_text(encoding="utf-8")


def _withheld_reason_keys() -> set[str]:
    """The keys of the panel's ``WITHHELD_REASONS`` table, parsed statically.

    ``app.js`` is a browser script with no module seam, so this is the same
    static-parsing approach ``tools/dev/check_panel.py`` already uses for the
    panel.  It is a drift guard, not a rendering test: it fails when the engine
    grows a refusal the panel would show as title-cased enum text.
    """
    script = _app_js()
    block = re.search(
        r"const WITHHELD_REASONS = \{(.*?)\n  \};", script, re.DOTALL
    )
    assert block is not None, "WITHHELD_REASONS table not found in app.js"
    return set(re.findall(r"^\s{4}([a-z0-9_]+):", block.group(1), re.MULTILINE))


def test_every_named_candidate_refusal_has_a_human_phrasing() -> None:
    """A refused fixture must not surface as ``Already Declared State Entity``.

    ``already_declared_state_entity`` was exactly that on production: it reached
    the Withheld list and was title-cased.  The candidate reasons are exported
    constants, so the test reads them rather than restating them — a new reason
    added to the engine fails here until the panel explains it.
    """
    from wattmosaic_engine.ingestion import candidate_entities

    constants = {
        name: value
        for name, value in vars(candidate_entities).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }
    assert constants, "candidate_entities exports no REASON_ constants"

    keys = _withheld_reason_keys()
    missing = sorted(set(constants.values()) - keys)
    assert not missing, f"no plain-English phrasing for {missing}"


def test_unknown_reason_codes_still_fall_back_to_title_case() -> None:
    """A newer engine's code must stay legible rather than render blank."""
    script = _app_js()
    assert "return titleCase(text);" in script
    assert "WITHHELD_REASONS[text] || titleCase(text)" not in script, (
        "the lookup must fall through to titleCase only for unknown codes, "
        "after the prose passthrough"
    )


def test_prefixed_withheld_reasons_are_handled() -> None:
    """The two parameterised reasons are not table keys, so pin their prefixes.

    ``main.py`` emits ``identified_by_aggregate_fit_on_<ct>`` and
    ``co_toggles_with_another_fixture_on_<ct>``.  A table lookup can never match
    them, and ``titleCase`` would render the CT slug inside a sentence.
    """
    engine = (REPO_ROOT / "engine" / "wattmosaic_engine" / "main.py").read_text(
        encoding="utf-8"
    )
    script = _app_js()
    for prefix in (
        "identified_by_aggregate_fit_on_",
        "co_toggles_with_another_fixture_on_",
    ):
        assert prefix in engine, f"{prefix} is no longer emitted by the engine"
        assert prefix in script, f"app.js does not phrase {prefix}"


def test_placed_empty_state_does_not_blame_the_evidence_bar_unconditionally() -> None:
    """The Placed slot must choose its message from why nothing is placed.

    On production a second Apply left ``accepted`` empty because every fixture
    was already written (``already_declared_state_entity``), yet the card said no
    fixture met the evidence bar.  The fix keys the message on that reason; this
    pins the branch so the false claim cannot come back silently.
    """
    script = _app_js()
    assert "isAlreadyWritten(reason)" in script, "empty state no longer reads the reason"
    assert "'already_declared_state_entity'" in script
    assert "already configured" in script
    # The evidence-bar claim survives only as the conservative fallback for
    # when no reason says "already written", and the old unconditional sentence
    # is gone.
    assert "No fixture reached the evidence bar for a measured placement." in script
    assert "No fixture met the evidence bar for a measured placement." not in script
    # The mixed branch (some already written, some not) must not excuse the
    # remainder with the evidence bar.  On a real 24 h response only 3 of 759
    # withheld+refused rows were previously applied fixtures; the rest were
    # ``mode_entity`` / ``not_a_load_*`` / ``wattmosaic_entity`` refusals that
    # were never load candidates, so they were never measured and cannot have
    # missed a bar.  Pin the branch by the false arm of the
    # ``everythingWritten`` ternary, which only exists for the mixed case.
    mixed = re.search(
        r"everythingWritten\s*\?\s*'(?P<all>[^']*)'\s*:\s*'(?P<mixed>[^']*)'",
        script,
    )
    assert mixed is not None, "the everythingWritten ternary is gone"
    assert "already written as a device" in mixed["mixed"]
    assert "evidence bar" not in mixed["mixed"], (
        "the mixed branch blames the evidence bar for rows that were never measured"
    )


def test_already_written_empty_states_do_not_claim_a_measurement() -> None:
    """Neither branch may say the run *proved* the fixtures it skipped.

    An already-declared entity is refused at the candidate layer, before any
    measurement, so "everything this window proved is already written" was
    vacuously true and implied a re-proof that never happened.  The mechanical
    fact (a duplicate refusal) is what the copy may state.  This is a
    source-shape guard, not a rendering test.
    """
    script = _app_js()
    assert "this window proved" not in script, (
        "the empty state claims the window re-proved rows that were refused "
        "before measurement"
    )
    mixed = re.search(
        r"everythingWritten\s*\?\s*'(?P<all>[^']*)'\s*:\s*'(?P<mixed>[^']*)'",
        script,
    )
    assert mixed is not None, "the everythingWritten ternary is gone"
    for arm in (mixed["all"], mixed["mixed"]):
        assert "duplicate" in arm, "the empty state does not state the mechanical refusal"
        assert "proved" not in arm
        assert "measured" not in arm


# --------------------------------------------------------------------------- #
# Discover-card honesty: apply toasts, refusal detail, and the notes footer
# --------------------------------------------------------------------------- #


def test_apply_toast_is_branched_on_the_applied_flag() -> None:
    """A 2xx is not a write, so the toast must read ``applied`` first.

    The engine answers a refusal with ``ok: true`` / ``applied: false`` / a
    ``detail``, and ``post()`` resolves for any 2xx.  Unconditionally toasting
    "Placements applied" therefore claimed a write that never happened.
    Source-shape guard only: it does not execute the button.
    """
    script = _app_js()
    handler = re.search(
        r"const result = await post\(DISCOVER_PATH, \{ apply: true \}\);(?P<body>.*?)\n        \} catch",
        script,
        re.DOTALL,
    )
    assert handler is not None, "the apply handler no longer inspects the response"
    body = handler.group("body")
    assert "result.applied === true" in body, (
        "the success toast must be conditioned on applied === true"
    )
    # The success text exists, but only inside the applied branch.
    applied_branch = body[body.index("result.applied === true") :]
    assert "'Placements applied'" in applied_branch
    assert "The engine wrote every proven placement" in applied_branch
    # The refusal branch says nothing about a write and relays ``detail``.
    refusal = applied_branch[applied_branch.index("} else {") :]
    assert "Nothing changed" in refusal
    assert "result.detail" in refusal
    for word in ("Placements applied", "wrote", "re-projected"):
        assert word not in refusal, f"the refusal toast claims {word!r}"


def test_detail_notice_is_not_gated_on_ok_false() -> None:
    """A refusal is ``ok: true``, so gating the explanation on ``ok === false``
    hid the one message that names the switch to turn on."""
    script = _app_js()
    assert "if (data.ok === false && data.detail)" not in script, (
        "the detail notice is still unreachable for an ``ok: true`` refusal"
    )
    assert "if (data.detail)" in script
    # Tone follows ``ok``: a real problem is an error, a refusal is advisory.
    assert (
        "data.ok === false\n          ? notice('Discovery reported a problem', "
        "String(data.detail), 'bad')"
    ) in script
    assert "notice('Nothing written yet', String(data.detail), 'warn')" in script


def test_engine_notes_are_phrased_not_joined_raw() -> None:
    """The footer used to join ~25 ``entity: code`` notes verbatim.

    That printed ``too_few_corroborations`` and friends in the most visible
    place on the card.  ``withheldReason`` already has a human phrasing for
    them, so the note formatter must route through it and must not simply
    ``notes.join(' ')``.  Source-shape guard.
    """
    script = _app_js()
    assert "`Engine notes: ${notes.join(' ')}`" not in script, (
        "the footer still joins raw reason codes"
    )
    formatter = script[
        script.index("function splitEntityNote(") : script.index(
            "function discoveryFoot("
        )
    ]
    assert "withheldReason(reason)" in formatter, (
        "the note formatter must phrase through withheldReason"
    )
    # Prose notes are passed through, decided by the same bare-code test
    # withheldReason uses -- a sentence must not be title-cased.
    assert "/[^a-z0-9_]/.test(reason)" in formatter
    assert "const parsed = splitEntityNote(note)" in formatter
    # Many notes are grouped by reason with counts instead of dumped.
    assert "NOTE_DETAIL_LIMIT" in formatter
    assert "not placed, by reason" in formatter


