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
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "tools" / "dev" / "check_release.py"


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
        '{\n  "domain": "wattmosaic",\n  "version": "%s"\n}\n' % version
    )
    (component / "const.py").write_text('VERSION = "%s"\nCONTRACT_VERSION = "1.2.0"\n' % version)
    addon = root / "addon"
    addon.mkdir(exist_ok=True)
    (addon / "config.yaml").write_text('name: "WattMosaic Engine"\nversion: "%s"\n' % version)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "wattmosaic-engine"\nversion = "%s"\n' % version
    )
    engine = root / "engine" / "wattmosaic_engine"
    engine.mkdir(parents=True, exist_ok=True)
    (engine / "__init__.py").write_text('__version__ = "%s"\n' % version)
    documented = notes_version if notes_version is not None else version
    (root / "RELEASE_NOTES.md").write_text(
        "# Release Notes\n\n## v%s — 2026-11-09\n\nSomething changed.\n" % documented
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


def test_a_prerelease_may_not_be_the_stable_channel(checker, tmp_path):
    """A ``-dev`` version on ``main`` would be offered to stable users."""
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev9")
    checker.check_channel("main")
    assert any("stable channel" in message for message in checker.ERRORS)


def test_a_prerelease_is_accepted_on_the_dev_channel(checker, tmp_path):
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "0.2.0-dev9")
    checker.check_channel("dev")
    assert checker.ERRORS == []


def test_a_stable_version_may_not_be_the_dev_channel(checker, tmp_path):
    """Stable code must not reach stable users through the dev channel."""
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "1.0.0")
    checker.check_channel("dev")
    assert any("development channel" in message for message in checker.ERRORS)


def test_a_stable_version_is_accepted_on_the_main_channel(checker, tmp_path):
    _retarget(checker, tmp_path)
    _write_tree(tmp_path, "1.0.0")
    checker.check_channel("main")
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

