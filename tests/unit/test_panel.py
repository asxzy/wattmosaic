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

The Discover-card copy has no JS test harness: ``addon/ui/app.js`` is a browser
script with no module seam, so those tests read the shipped source and pin its
shape (the same static approach ``tools/dev/check_panel.py`` uses).  They are
drift guards for user-visible wording, not rendering tests.
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
        r"const result = await post\(DISCOVER_PATH, \{.*?\}\);(?P<body>.*?)\n        \} catch",
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


def test_detail_notice_is_gated_on_a_hard_precondition() -> None:
    """Only ``ok: false`` may put ``detail`` on the card.

    ``e1448a8`` widened this gate to "is there a detail?", which made every
    ordinary read-only Discover that found a fixture render
    "Nothing written yet -- apply is false; pass apply=true ...".  The user
    pressed Discover; they never attempted a write, so a warning about not
    having written is a false alarm.  A refusal is ``ok: true`` /
    ``applied: false`` and its explanation belongs to the Apply toast (pinned
    by ``test_apply_toast_is_branched_on_the_applied_flag``), which is the only
    place the user can learn which switch to turn on.
    Source-shape guard only: there is no JS test harness for this panel.
    """
    script = _app_js()
    assert "if (data.ok === false && data.detail)" in script, (
        "the card notice must fire only for a hard precondition"
    )
    assert "if (data.detail) {" not in script, (
        "the card notice is ungated again, so a read-only refusal warns"
    )
    assert "notice('Nothing written yet'" not in script, (
        "a refusal is rendered on the analysis card instead of the apply toast"
    )
    assert "notice('Discovery reported a problem', String(data.detail), 'bad')" in script


def test_refusal_detail_reaches_the_user_through_the_apply_toast() -> None:
    """Gating the card must not drop the refusal explanation.

    The only way to learn "auto placement is disabled: turn on the
    'auto_placement' App option" is the apply toast, because the engine only
    emits that sentence when a write was attempted.  The toast must relay
    ``detail`` and must not claim anything was written.
    """
    script = _app_js()
    handler = re.search(
        r"const result = await post\(DISCOVER_PATH, \{.*?\}\);(?P<body>.*?)\n        \} catch",
        script,
        re.DOTALL,
    )
    assert handler is not None, "the apply handler no longer inspects the response"
    body = handler.group("body")
    refusal = body[body.index("} else {") :]
    assert "result.detail" in refusal, "the refusal toast does not relay the engine's detail"
    assert "Nothing changed" in refusal
    assert "No placement was made; the topology is unchanged." in refusal
    # The card keeps the prior analysis after a refusal, so the toast is the
    # only surface for the explanation -- it must not be blanked or blank-capable.
    assert "state.discover = { ...state.discover, running: false }" in body, (
        "a refusal must keep the analysis on the card, not blank it"
    )
    for word in ("Placements applied", "wrote", "re-projected"):
        assert word not in refusal, f"the refusal toast claims {word!r}"


def test_hard_precondition_sentence_is_not_repeated_in_the_footer() -> None:
    """``ok: false`` carries its sentence in both ``detail`` and ``notes``.

    The notice renders ``detail``; the footer renders ``notes``.  Without a
    dedupe the card shows "no topology to place devices on" twice, once as an
    error and once as an engine note.
    """
    script = _app_js()
    footer = script[script.index("function discoveryFoot(") : script.index("function discoverPlacedRow(")]
    assert "String(note) === String(data.detail)" in footer, (
        "the footer does not drop a note that repeats the detail notice"
    )


def test_all_empty_discovery_does_not_claim_a_measurement_when_it_could_not_run() -> None:
    """An ``ok: false`` run must not be described as a comparison.

    ``POST /api/v1/discover/placements`` answers a hard precondition (no active
    topology) with ``ok: false``, ``detail`` "no topology to place devices on"
    and *every* collection empty.  That is indistinguishable from a run that
    finished and proved nothing by looking at the lists alone, so the card used
    to say the engine "compared every fixture transition ... and found nothing
    it could prove. That is a measurement result".  With no topology the engine
    returns before it reads a transition, so that sentence claims a measurement
    that never happened.  The empty state must branch on ``ok``.

    Source-shape guard only: this panel has no JS test harness, so the branch is
    pinned statically, like the other guards in this module.
    """
    script = _app_js()
    start = script.index("if (nothing) {")
    end = script.index("return card('Discover devices', body, {", start)
    block = script[start:end]
    arms = re.search(
        r"if \(data\.ok === false\) \{(?P<hard>.*?)\n      \} else \{(?P<ran>.*?)\n      \}",
        block,
        re.DOTALL,
    )
    assert arms is not None, (
        "the all-empty empty state no longer branches on ok, so a hard "
        "precondition renders the measurement verdict again"
    )
    hard, ran = arms["hard"], arms["ran"]
    assert "'Discovery could not run'" in hard
    for claim in (
        "found nothing it could prove",
        "measurement result",
        "compared",
        "measured",
    ):
        assert claim not in hard, (
            f"the ok:false empty state still claims a measurement ({claim!r})"
        )
    # The reason lives in the notice above; repeating the sentence is the
    # duplicate ``discoveryFoot`` already filters out of the footer.  Compare
    # rendered strings only: the branch's own comments quote the engine detail.
    rendered = "\n".join(
        line for line in block.splitlines() if not line.strip().startswith("//")
    )
    assert "no topology to place devices on" not in rendered
    # A completed run that proved nothing keeps its accurate wording.
    assert "'No shared circuits could be resolved'" in ran
    assert "found nothing it could prove" in ran
    assert "measurement result" in ran


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


# --------------------------------------------------------------------------- #
# Discover-card window selector
#
# ``addon/ui/app.js`` is a browser script with no module seam, so there is no JS
# unit harness to call ``discoverWindow()`` (the same reason every guard in this
# module reads the source).  These are static drift guards: they pin that the
# request body carries a *selected* window, that a long option exists, and that
# the write path reuses the window the reviewed run used.  Rendering, the
# ``<select>`` itself, and the elapsed clock are not executed here.
# --------------------------------------------------------------------------- #


def _discover_window_options() -> list[tuple[int, str]]:
    """The ``(hours, label)`` pairs of the panel's ``DISCOVER_WINDOWS`` table.

    Parsed statically for the same reason as ``_withheld_reason_keys``.  It
    fails loudly if the table is reshaped, because the guards below lean on the
    options actually shipped rather than a restated copy.
    """
    script = _app_js()
    block = re.search(
        r"const DISCOVER_WINDOWS = Object\.freeze\(\[(.*?)\n  \]\);",
        script,
        re.DOTALL,
    )
    assert block is not None, "DISCOVER_WINDOWS table not found in app.js"
    return [
        (int(hours), label)
        for hours, label in re.findall(
            r"\{ hours: (\d+), label: '([^']*)'", block.group(1)
        )
    ]


def test_discover_window_options_cover_the_useful_windows() -> None:
    """The card must offer the long windows, not just the 24 h default.

    On production a 24 h window found 0 groups on light CTs, 48 h found 10 and
    168 h found 60.  Without a long option in the selector the shared
    light-circuit disaggregation the engine can do is unreachable from the UI.
    """
    options = _discover_window_options()
    hours = [value for value, _ in options]
    assert hours == [24, 48, 72, 168], f"unexpected windows: {hours}"
    # A 72/168-style option exists, and the week option says so in words.
    assert 168 in hours
    week = dict(options)[168]
    assert "7 days" in week and "168" in week
    # The default is an explicit constant, not a bare literal at the call site,
    # and it is the fast first run.
    script = _app_js()
    assert "const DISCOVER_DEFAULT_HOURS = 24;" in script


#: The pure window helper block: the table, the default and the three lookups.
#: It is sliced out of the IIFE because the panel has no module seam, and it is
#: *executed* under node rather than pattern-matched -- this is the one
#: behaviour test in this section, the rest are source-shape guards.
_WINDOW_HELPER_START = "const DISCOVER_WINDOWS"
_WINDOW_HELPER_END = "/* ---------------------------------------------------------------------- *\n   * Application state"


def _node_window_helpers():
    """Load ``DISCOVER_WINDOWS`` and its helpers into node and report them.

    Returns ``None`` when node is not installed: the panel itself does not need
    node at runtime, so a missing toolchain skips this test instead of failing
    the suite, exactly as ``scripts/validate_addon.sh`` does for ``node --check``.
    """
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return None
    script = _app_js()
    start = script.index(_WINDOW_HELPER_START)
    end = script.index(_WINDOW_HELPER_END, start)
    block = script[start:end]
    program = (
        "const num = (v, d = 0) => String(v);\n"
        f"{block}\n"
        "const hours = [24, 48, 72, 168, 12, 2160];\n"
        "console.log(JSON.stringify({\n"
        "  options: DISCOVER_WINDOWS.map((o) => ({ hours: o.hours, label: o.label })),\n"
        "  defaultHours: DISCOVER_DEFAULT_HOURS,\n"
        "  long: hours.map((h) => [h, discoverLongRun(h)]),\n"
        "  labels: hours.map((h) => [h, discoverWindowLabel(h)]),\n"
        "  known: hours.map((h) => [h, Boolean(discoverWindow(h))]),\n"
        "}));\n"
    )
    completed = subprocess.run(
        [node, "-e", program],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_window_helper_lookups_and_long_threshold_behaviour() -> None:
    """Execute the pure window helper in node and pin what it returns.

    Unlike the request-flow guards in this section, this is a behaviour test:
    the helper is sliced out of the IIFE (the panel has no module seam) and run
    under node.  It pins the offered windows, the 24 h default, that 72 h and
    168 h are treated as long waits, and that an unlisted window still gets a
    readable label rather than ``undefined``.  Skipped without node, which is
    not needed to serve the panel.
    """
    result = _node_window_helpers()
    if result is None:
        pytest.skip("node is not installed; the panel does not need it at runtime")

    assert [option["hours"] for option in result["options"]] == [24, 48, 72, 168]
    assert result["defaultHours"] == 24
    # The pairs come back as lists, so coerce the hours back to ``int``.
    assert {int(hours): flag for hours, flag in result["long"]} == {
        24: False, 48: False, 72: True, 168: True,
        12: False, 2160: True,
    }
    labels = {int(hours): text for hours, text in result["labels"]}
    assert labels[24] == "24 h"
    assert labels[168] == "7 days (168 h)"
    # An unlisted but valid engine window is still labelled, never "undefined".
    assert labels[12] == "12 h"
    assert labels[2160] == "2160 h"
    known = {int(hours): flag for hours, flag in result["known"]}
    assert known[168] is True
    assert known[12] is False



def test_discover_request_posts_the_selected_window() -> None:
    """The read-only body must carry ``lookback_hours`` from the selector.

    The shipped card posted a literal 24 h.  The engine accepts up to 2160 h and
    its evidence grows strongly with the window, so the request must read the
    selection instead of a hardcoded value.  It must still be read-only:
    ``apply: false`` stays in the same body.
    """
    script = _app_js()
    call = re.search(
        r"const data = await post\(DISCOVER_PATH, \{(?P<body>.*?)\n      \}\);",
        script,
        re.DOTALL,
    )
    assert call is not None, "the read-only discover request moved or was removed"
    body = call.group("body")
    assert "apply: false" in body, "discovery must stay read-only"
    assert "lookback_hours" in body, (
        "the discover body no longer sends a lookback window"
    )
    assert "lookback_hours: hours" in body, (
        "the request must use the selected window, not a literal"
    )
    # The pinned value comes from the selection read before the request.
    assert "const hours = state.discover.window;" in script
    # No literal 24 survives as the request's window.
    assert "lookback_hours: 24" not in script


def test_apply_request_reuses_the_reviewed_window() -> None:
    """An apply must write the window the card's analysis was run with.

    Showing evidence for one window and writing another is the failure this
    pins against: ``runDiscovery`` sets ``ranWindow`` and the apply body reads
    it, so a later selector change cannot move the write off the reviewed
    window.
    """
    script = _app_js()
    call = re.search(
        r"const result = await post\(DISCOVER_PATH, \{(?P<body>.*?)\n          \}\);",
        script,
        re.DOTALL,
    )
    assert call is not None, "the apply request moved or was removed"
    body = call.group("body")
    assert "apply: true" in body
    assert "lookback_hours" in body, "the apply request dropped the window"
    assert "state.discover.ranWindow" in body, (
        "apply must use the window the reviewed analysis ran with"
    )
    # The run pins that window before the read-only request is sent.
    run = script[script.index("async function runDiscovery()") : script.index("function discoverCard(")]
    assert "ranWindow: hours" in run, "runDiscovery does not pin the window it reads"
    # Both call sites must agree: the same pinned field feeds discover, footer
    # and apply, so there is one selected window rather than two opinions.
    assert body.count("lookback_hours") == 1


def test_long_window_loading_state_says_the_wait_is_long() -> None:
    """168 h measured 155 s, so a silent card looks dead.

    The loading path must keep its spinner and state the long wait in words,
    without inventing a progress percentage.  The elapsed clock reports real
    time only.
    """
    script = _app_js()
    loading = script[
        script.index("function discoverLoadingCard()") : script.index(
            "function discoverErrorCard("
        )
    ]
    assert "spinner" in loading, "the loading state lost its liveness animation"
    assert "discoverLongRun(hours)" in loading, "the long-window branch is gone"
    assert "2.5 minutes" in loading, "the long-window wait is not stated in words"
    assert "topology-discover-elapsed" in loading, "the elapsed clock is gone"
    assert "duration((Date.now() - startedAt) / 1000)" in script, (
        "the elapsed clock does not count real elapsed time"
    )
    # Compare rendered strings and calls only: the comments *say* there is no
    # percentage, so searching the raw block would match its own denial.
    rendered = "\n".join(
        line for line in loading.splitlines() if not line.strip().startswith("//")
    )
    assert not re.search(r"percent\(|progress|width:\s*['\"`]", rendered), (
        "the loading state fabricates a percentage or a progress bar"
    )
    # The selector is disabled for the duration of a run and restored after it.
    assert "windowSelect.disabled = false;" in script
