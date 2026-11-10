"""Tests for the release contract checker.

The checker exists because this repository published fifteen versions with no
git tag and no GitHub Release, so Home Assistant could not name a version at
all: it fell back to the branch head and reported a raw commit SHA.  The code
was correct and the *metadata* was missing, which no existing gate noticed.

Each test below pins one of the rules that would have caught that, and the
negative tests prove the rule fires rather than merely existing.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "tools" / "dev" / "check_release.py"

#: The SemVer 2.0.0 grammar, duplicated here on purpose: an ordering test that
#: reused the checker's own regex could not detect a wrong grammar.
CHECKER_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _load_checker():
    spec = importlib.util.spec_from_file_location("wm_check_release", CHECKER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["wm_check_release"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def checker(tmp_path, monkeypatch):
    """A checker instance pointed at a throwaway tree.

    The real tree is only read; every failing case is built in ``tmp_path`` so a
    deliberately broken version field can never be mistaken for the repository's
    own state.
    """
    module = _load_checker()
    monkeypatch.setattr(module, "ERRORS", [])
    monkeypatch.setattr(module, "NOTES_OUT", [])
    return module


def _write_tree(root: Path, version: str, notes_version: str | None = None) -> None:
    component = root / "custom_components" / "wattmosaic"
    component.mkdir(parents=True, exist_ok=True)
    (component / "manifest.json").write_text(
        json.dumps({"domain": "wattmosaic", "version": version}, indent=2) + "\n"
    )
    (component / "const.py").write_text(
        f'VERSION = "{version}"\nCONTRACT_VERSION = "1.2.0"\n'
    )
    addon = root / "addon"
    addon.mkdir(exist_ok=True)
    (addon / "config.yaml").write_text(
        f'name: "WattMosaic Engine"\nversion: "{version}"\n'
    )
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "wattmosaic-engine"\nversion = "{version}"\n'
    )
    engine = root / "engine" / "wattmosaic_engine"
    engine.mkdir(parents=True, exist_ok=True)
    (engine / "__init__.py").write_text(f'__version__ = "{version}"\n')
    documented = notes_version if notes_version is not None else version
    (root / "RELEASE_NOTES.md").write_text(
        f"# Release Notes\n\n## v{documented} — 2026-11-09\n\nSomething changed.\n"
    )


def _retarget(module, root: Path) -> None:
    module.ROOT = root
    module.MANIFEST = root / "custom_components" / "wattmosaic" / "manifest.json"
    module.CONST = root / "custom_components" / "wattmosaic" / "const.py"
    module.ADDON = root / "addon" / "config.yaml"
    module.PYPROJECT = root / "pyproject.toml"
    module.PACKAGE_INIT = root / "engine" / "wattmosaic_engine" / "__init__.py"
    module.NOTES = root / "RELEASE_NOTES.md"


def test_a_consistent_tree_passes(checker, tmp_path):
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev9")
    assert checker.check_version_fields() == "0.2.0-dev9"
    checker.check_notes_cover_current()
    assert checker.ERRORS == []


def test_a_stale_pyproject_version_is_rejected(checker, tmp_path):
    """The exact drift this repository had: pyproject stuck at 0.2.0-dev4."""
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev9")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "wattmosaic-engine"\nversion = "0.2.0-dev4"\n'
    )
    checker.check_version_fields()
    assert any("disagree" in message for message in checker.ERRORS)


def test_a_stale_package_version_is_rejected(checker, tmp_path):
    """``__version__`` drifted from 0.1.3 for three releases unnoticed."""
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev9")
    (tmp_path / "engine" / "wattmosaic_engine" / "__init__.py").write_text(
        '__version__ = "0.1.3"\n'
    )
    checker.check_version_fields()
    assert any("disagree" in message for message in checker.ERRORS)


def test_a_tag_must_match_the_tree_version(checker, tmp_path):
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev9")
    checker.check_tag("v0.1.5")
    assert any("says 0.1.5 but the tree says 0.2.0-dev9" in message for message in checker.ERRORS)


def test_a_matching_tag_is_accepted(checker, tmp_path):
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev9")
    checker.check_tag("v0.2.0-dev9")
    assert checker.ERRORS == []


def test_a_prerelease_tag_is_flagged_as_prerelease(checker, tmp_path):
    """A ``-dev`` tag must not become the stable 'latest' release."""
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev9")
    checker.check_tag("v0.2.0-dev9")
    assert any("prerelease" in message for message in checker.NOTES_OUT)


def test_a_tag_without_a_tag_prefix_is_rejected(checker, tmp_path):
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev9")
    checker.check_tag("0.2.0-dev9")
    assert any("must start with 'v'" in message for message in checker.ERRORS)


def test_an_untagged_version_must_be_documented(checker, tmp_path):
    """The release notes are the only human record of what shipped."""
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev9", notes_version="0.2.0-dev8")
    checker.check_notes_cover_current()
    assert any("does not document the current version" in message for message in checker.ERRORS)


def test_a_tag_needs_a_release_notes_section(checker, tmp_path):
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev9", notes_version="0.2.0-dev8")
    checker.check_tag("v0.2.0-dev9")
    assert any("no '## v0.2.0-dev9' section" in message for message in checker.ERRORS)


def test_the_real_tree_is_consistent():
    """Guard the repository itself, not a fixture.

    This is the assertion that would have failed for every one of the fifteen
    published versions: the version fields disagreed and nothing noticed.
    """
    module = _load_checker()
    module.ERRORS = []
    module.NOTES_OUT = []
    version = module.check_version_fields()
    module.check_notes_cover_current()
    assert module.ERRORS == [], module.ERRORS
    assert version is not None


def test_a_prerelease_is_accepted_on_the_main_channel(checker, tmp_path):
    """``main`` is the development line, so a prerelease is what it carries.

    This is not a relaxation of safety: the Marketplace ignores prereleases
    unless the beta toggle is enabled, so a ``-dev.N`` on ``main`` cannot reach a
    stable user.
    """
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev.9")
    checker.check_channel("main")
    assert checker.ERRORS == []


def test_a_stable_version_may_not_be_the_main_channel(checker, tmp_path):
    """A stable version on the development line would be offered to everyone."""
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "1.0.0")
    checker.check_channel("main")
    assert any("development channel" in message for message in checker.ERRORS)


def test_a_prerelease_may_not_be_the_stable_channel(checker, tmp_path):
    """A ``-dev`` version on ``stable`` would be offered to stable users."""
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev.9")
    checker.check_channel("stable")
    assert any("stable channel" in message for message in checker.ERRORS)


def test_a_stable_version_is_accepted_on_the_stable_channel(checker, tmp_path):
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "1.0.0")
    checker.check_channel("stable")
    assert checker.ERRORS == []


def test_the_release_body_is_taken_from_the_notes(checker, tmp_path):
    """The Release page must not retype what the repository already records."""
    _retarget(checker, tmp_path)
    (tmp_path / "RELEASE_NOTES.md").write_text(
        "# Release Notes\n"
        "\n"
        "## v1.0.0 — 2026-11-09\n"
        "\n"
        "The body of the release.\n"
        "\n"
        "### Fixed\n"
        "\n"
        "- One thing.\n"
        "\n"
        "---\n"
        "\n"
        "## v0.9.0 — 2026-11-01\n"
        "\n"
        "An older body.\n"
    )
    assert checker.notes_section("1.0.0") == (
        "The body of the release.\n\n### Fixed\n\n- One thing.\n"
    )
    assert checker.notes_section("9.9.9") is None


def test_the_release_body_stops_at_the_next_section(checker, tmp_path):
    """A boundary must not leak the previous release into the new one."""
    _retarget(checker, tmp_path)
    (tmp_path / "RELEASE_NOTES.md").write_text(
        "# Release Notes\n\n## v1.0.0\n\nNew.\n\n## v0.9.0\n\nOld.\n"
    )
    body = checker.notes_section("1.0.0")
    assert body == "New.\n"
    assert "Old." not in body


def test_a_glued_prerelease_counter_is_rejected(checker, tmp_path):
    """The exact defect: ``0.2.0-dev10`` sorts BELOW ``0.2.0-dev9``.

    SemVer compares an identifier containing letters lexically, so the tenth
    dev build becomes invisible to any updater that orders by version.  The
    repository shipped dev1..dev9 before this was noticed; dev10 would have been
    the release that silently never installed.
    """
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev10")
    checker.check_version_fields()
    assert any("glues a 2-digit counter" in message for message in checker.ERRORS)


def test_a_single_digit_glued_counter_is_a_warning_not_an_error(checker, tmp_path):
    """``-dev9`` orders correctly today; it is a trap, not a defect yet."""
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev9")
    checker.check_version_fields()
    assert checker.ERRORS == []
    assert any("glued counter" in message for message in checker.NOTES_OUT)


def test_a_dot_separated_counter_is_accepted(checker, tmp_path):
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.1-dev.10")
    checker.check_version_fields()
    assert checker.ERRORS == []
    assert checker.NOTES_OUT == []


def test_a_purely_numeric_prerelease_is_accepted(checker, tmp_path):
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-1")
    checker.check_version_fields()
    assert checker.ERRORS == []
    assert checker.NOTES_OUT == []


def test_a_plain_stable_version_has_no_prerelease_problem(checker, tmp_path):
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "1.2.3")
    checker.check_version_fields()
    assert checker.ERRORS == []
    assert checker.NOTES_OUT == []


def test_a_glued_counter_tag_is_rejected(checker, tmp_path):
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev10")
    checker.check_tag("v0.2.0-dev10")
    assert any("glues a 2-digit counter" in message for message in checker.ERRORS)


def test_the_dot_separated_counter_actually_orders_above_the_previous():
    """Pin the arithmetic, not just the shape.

    If this ever fails, the whole ``-dev.N`` channel stops incrementing, which
    is the defect this file exists to prevent.
    """

    def semver_key(version: str):
        match = CHECKER_SEMVER.match(version)
        assert match is not None, version
        core = tuple(int(match.group(k)) for k in ("major", "minor", "patch"))
        pre = match.group("pre")
        if pre is None:
            return core + ((1,),)
        parts = tuple(
            (0, int(x), "") if x.isdigit() else (1, 0, x) for x in pre.split(".")
        )
        return core + ((0, parts),)

    assert semver_key("0.2.1-dev.10") > semver_key("0.2.1-dev.9")
    assert semver_key("0.2.1-dev.2") > semver_key("0.2.1-dev.1")
    assert semver_key("0.2.1") > semver_key("0.2.1-dev.10")
    # And the form that broke it, which single digits hide:
    assert semver_key("0.2.0-dev10") < semver_key("0.2.0-dev9")
    assert semver_key("0.2.0-dev9") > semver_key("0.2.0-dev8")
    # Escaping an existing glued series needs a new core version: turning the
    # separator into a dot is not sufficient on its own.
    assert semver_key("0.2.1-dev.1") > semver_key("0.2.0-dev9")


def test_the_real_tree_does_not_use_a_glued_counter():
    """Guard the repository's own version, not a fixture."""
    module = _load_checker()
    module.ERRORS = []
    module.NOTES_OUT = []
    module.check_version_fields()
    module.check_notes_cover_current()
    assert module.ERRORS == [], module.ERRORS


def test_the_served_engine_version_is_the_package_version():
    """`/health`, `/status` and metrics must not report a hardcoded version.

    ``api/facade.py`` used to define ``ENGINE_VERSION = "0.1.2"`` and
    ``contracts.HealthResponse`` defaulted to the same literal, while
    ``__version__`` had moved on.  Nothing failed, because the release checker
    only compared the five version *fields* and this was a sixth literal.  The
    panel's Diagnostics view made the drift user-visible, so the constant now
    derives from ``__version__`` and this test pins that.
    """
    engine_root = ROOT / "engine"
    if str(engine_root) not in sys.path:
        sys.path.insert(0, str(engine_root))

    from wattmosaic_engine import __version__
    from wattmosaic_engine.api import facade
    from wattmosaic_engine.contracts import HealthResponse

    assert __version__ == facade.ENGINE_VERSION
    assert __version__ == HealthResponse().engine_version
    assert "0.1.2" not in (ROOT / "engine/wattmosaic_engine/api/facade.py").read_text()


def test_the_health_schema_does_not_embed_the_current_version():
    """The generated schema must not change when only the version changes.

    CI bumps the version on every push to ``main`` and then asserts
    ``git diff --exit-code data/schemas``.  A plain pydantic default writes the
    current version into the JSON schema, so every auto-release would fail the
    schema gate with no contract change.  ``HealthResponse.engine_version``
    uses a ``default_factory`` for exactly that reason: the runtime field is
    still filled in, but no value appears in the generated schema.
    """
    engine_root = ROOT / "engine"
    if str(engine_root) not in sys.path:
        sys.path.insert(0, str(engine_root))

    from wattmosaic_engine import __version__
    from wattmosaic_engine.contracts import HealthResponse

    prop = HealthResponse.model_json_schema()["properties"]["engine_version"]
    assert "default" not in prop, prop
    assert HealthResponse().engine_version == __version__
