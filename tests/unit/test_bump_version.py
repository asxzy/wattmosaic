"""The automatic development-version bump.

``main`` is now the development line and every push publishes a prerelease, so
the version has to be incremented by CI rather than by hand.  The Supervisor
only offers an App update when ``addon/config.yaml``'s ``version:`` *string*
changes -- it compares versions, never commits -- so a push that changes code
without moving the version builds an image nobody is offered, and the installed
App keeps running old code while the workflow reports success.  That is the
``0.2.0-dev5`` defect.

What is pinned here:

* the increment is ``X.Y.Z-dev.N`` -> ``X.Y.Z-dev.(N+1)``, with the counter as a
  separate numeric identifier so ``dev.10`` still sorts above ``dev.9``;
* a **stable** version is refused, because ``X.Y.Z-dev.1`` sorts *below*
  ``X.Y.Z`` and would never be offered;
* a glued counter (``dev9``) is refused for the same ordering reason;
* all five version fields are rewritten together, so the release contract
  cannot be left half-bumped;
* nothing else in those files changes.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUMPER = ROOT / "tools" / "dev" / "bump_version.py"


def _load():
    spec = importlib.util.spec_from_file_location("wm_bump_version", BUMPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["wm_bump_version"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def bumper():
    return _load()


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    """A minimal copy of the five version fields, retargeted at ``tmp_path``."""
    module = _load()
    (tmp_path / "custom_components" / "wattmosaic").mkdir(parents=True)
    (tmp_path / "addon").mkdir()
    (tmp_path / "engine" / "wattmosaic_engine").mkdir(parents=True)

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(
        module, "MANIFEST", tmp_path / "custom_components/wattmosaic/manifest.json"
    )
    monkeypatch.setattr(
        module, "CONST", tmp_path / "custom_components/wattmosaic/const.py"
    )
    monkeypatch.setattr(module, "ADDON", tmp_path / "addon/config.yaml")
    monkeypatch.setattr(module, "PYPROJECT", tmp_path / "pyproject.toml")
    monkeypatch.setattr(
        module, "PACKAGE_INIT", tmp_path / "engine/wattmosaic_engine/__init__.py"
    )
    monkeypatch.setattr(module, "NOTES", tmp_path / "RELEASE_NOTES.md")
    _write_tree(module, tmp_path, "0.2.1-dev.1")
    return module, tmp_path


def _write_tree(module, root: Path, version: str) -> None:
    (root / "custom_components/wattmosaic/manifest.json").write_text(
        json.dumps({"domain": "wattmosaic", "version": version}, indent=2) + "\n"
    )
    (root / "custom_components/wattmosaic/const.py").write_text(
        f'DOMAIN = "wattmosaic"\nVERSION = "{version}"\n'
    )
    (root / "addon/config.yaml").write_text(
        f'name: "WattMosaic Engine"\nversion: "{version}"\nslug: "wattmosaic_engine"\n'
    )
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "wattmosaic"\nversion = "{version}"\n'
    )
    (root / "engine/wattmosaic_engine/__init__.py").write_text(
        f'"""Engine."""\n\n__version__ = "{version}"\n'
    )
    (root / "RELEASE_NOTES.md").write_text(
        "# Release Notes\n\n"
        f"## v{version} — 2026-11-09 (development channel)\n\n"
        "Something changed.\n"
    )


class TestNextVersion:
    def test_the_counter_increments(self, bumper):
        assert bumper.next_version("0.2.1-dev.1") == "0.2.1-dev.2"

    def test_a_single_digit_becomes_two(self, bumper):
        """The transition that used to break the channel."""
        assert bumper.next_version("0.2.1-dev.9") == "0.2.1-dev.10"

    def test_two_digits_keep_incrementing(self, bumper):
        assert bumper.next_version("0.2.1-dev.99") == "0.2.1-dev.100"

    def test_the_counter_stays_its_own_identifier(self, bumper):
        """``dev.10`` must not become ``dev10``: that would sort below ``dev9``."""
        result = bumper.next_version("0.2.1-dev.9")
        assert re.search(r"-dev\.\d+$", result), result

    def test_build_metadata_is_preserved(self, bumper):
        """It does not affect precedence, but it is part of what a build claims."""
        assert bumper.next_version("0.2.1-dev.1+build.5") == "0.2.1-dev.2+build.5"

    @pytest.mark.parametrize("version", ["0.1.6", "1.0.0", "0.2.0"])
    def test_a_stable_version_is_refused(self, bumper, version):
        """``X.Y.Z-dev.1`` sorts BELOW ``X.Y.Z``, so no updater would offer it."""
        with pytest.raises(bumper.BumpError) as excinfo:
            bumper.next_version(version)
        assert "sorts BELOW" in str(excinfo.value)

    @pytest.mark.parametrize("version", ["0.2.0-dev9", "0.2.0-dev10"])
    def test_a_glued_counter_is_refused(self, bumper, version):
        with pytest.raises(bumper.BumpError) as excinfo:
            bumper.next_version(version)
        assert "dot form" in str(excinfo.value)

    @pytest.mark.parametrize("version", ["0.2.1-rc.1", "1.0.0-beta.2"])
    def test_another_channel_is_refused(self, bumper, version):
        """Only the dev counter is ours to move; rc/beta are a human decision."""
        with pytest.raises(bumper.BumpError):
            bumper.next_version(version)

    def test_nonsense_is_refused(self, bumper):
        with pytest.raises(bumper.BumpError):
            bumper.next_version("not-a-version")


class TestWriteVersions:
    def test_every_field_moves_together(self, tree):
        module, root = tree
        target = module.next_version(module.current_version(root))
        module.write_versions(target, root)

        assert module.current_version(root) == target
        for relative in (
            "custom_components/wattmosaic/manifest.json",
            "custom_components/wattmosaic/const.py",
            "addon/config.yaml",
            "pyproject.toml",
            "engine/wattmosaic_engine/__init__.py",
        ):
            assert target in (root / relative).read_text(), relative

    def test_nothing_else_in_the_file_changes(self, tree):
        """A blind string replace would rewrite a version mentioned in a comment."""
        module, root = tree
        const = root / "custom_components/wattmosaic/const.py"
        const.write_text(
            'DOMAIN = "wattmosaic"\n'
            'VERSION = "0.2.1-dev.1"\n'
            "# NOTE: 0.2.1-dev.1 shipped the first fix\n"
        )
        module.write_versions("0.2.1-dev.2", root)

        text = const.read_text()
        assert 'VERSION = "0.2.1-dev.2"' in text
        assert "# NOTE: 0.2.1-dev.1 shipped the first fix" in text

    def test_the_manifest_stays_valid_json(self, tree):
        module, root = tree
        module.write_versions("0.2.1-dev.2", root)
        data = json.loads(
            (root / "custom_components/wattmosaic/manifest.json").read_text()
        )
        assert data["version"] == "0.2.1-dev.2"
        assert data["domain"] == "wattmosaic"

    def test_disagreeing_fields_are_refused(self, tree):
        """Bumping a tree that is already inconsistent would hide the drift."""
        module, root = tree
        (root / "pyproject.toml").write_text(
            '[project]\nname = "wattmosaic"\nversion = "9.9.9"\n'
        )
        with pytest.raises(module.BumpError) as excinfo:
            module.current_version(root)
        assert "disagree" in str(excinfo.value)


class TestReleaseNotes:
    def test_the_new_section_goes_above_the_previous_one(self, tree):
        """Newest first is the file's existing convention."""
        module, root = tree
        module.append_notes("0.2.1-dev.2", "Fixed a thing.", root=root)
        text = (root / "RELEASE_NOTES.md").read_text()
        assert text.index("## v0.2.1-dev.2") < text.index("## v0.2.1-dev.1")

    def test_the_body_is_preserved_verbatim(self, tree):
        module, root = tree
        module.append_notes("0.2.1-dev.2", "- one\n- two", root=root)
        text = (root / "RELEASE_NOTES.md").read_text()
        assert "- one\n- two" in text

    def test_the_version_becomes_documented(self, tree):
        """``check_release.py`` refuses to tag a version the notes omit."""
        module, root = tree
        module.append_notes("0.2.1-dev.2", "Body.", root=root)
        documented = {
            m.group(1)
            for m in re.finditer(
                r"^##\s+v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)",
                (root / "RELEASE_NOTES.md").read_text(),
                re.MULTILINE,
            )
        }
        assert "0.2.1-dev.2" in documented
