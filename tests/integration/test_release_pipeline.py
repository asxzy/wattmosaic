"""Guard the release pipeline against publishing a stale, mismatched tag.

Why this file exists
--------------------
``0.2.0-dev5`` was published from commit ``7a0922d``.  A later commit
``69ebbfb`` fixed two accounting defects and pushed to ``dev`` **without
bumping** ``version:`` in ``addon/config.yaml``.  The image workflow ran, saw
that the tag already existed, published nothing, and still reported success.
The live app therefore kept running the pre-fix code while the HA update entity
advertised ``0.2.0-dev5`` -- a silent "the fix shipped" lie.

The pipeline now stamps ``org.opencontainers.image.revision`` into every image
and refuses to treat an existing tag as up to date unless that label matches the
current commit.  These tests pin the workflow contract, because the failure mode
is an *absence* of work that no runtime assertion can observe.

The image steps moved from ``app-image.yaml`` into the ``image`` job of
``release.yaml``: a version bump has to be committed by CI, and a push made with
the automatic ``GITHUB_TOKEN`` does not trigger another workflow, so the build
had to become part of the same run.  The assertions below are unchanged; only
the file and job they read have moved.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yaml"
JOB = "image"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps() -> list[dict]:
    return _workflow()["jobs"][JOB]["steps"]


def _step(prefix: str) -> dict:
    for step in _steps():
        if (step.get("name") or "").startswith(prefix):
            return step
    raise AssertionError(f"workflow has no step named {prefix!r}")


def test_image_is_stamped_with_the_building_commit() -> None:
    """Without a revision label the stale-tag check has nothing to compare."""
    build = _step("Build and push")
    labels = build["with"]["labels"]
    # The build runs against the bumped commit, which is what the tag names.
    assert "org.opencontainers.image.revision=${{ needs.prepare.outputs.sha }}" in labels


def test_existing_tag_is_checked_against_this_commit() -> None:
    """An existing tag must be verified, not assumed to contain this commit."""
    step = _step("Refuse a stale tag")
    script = step["run"]
    assert "org.opencontainers.image.revision" in script
    assert "needs.prepare.outputs.sha" in script
    # A mismatch must fail the job: exiting 0 here is the original defect.
    assert re.search(r"exit\s+1", script), script
    # It must only run when the tag already exists (the build path is untouched).
    assert "exists.outputs.exists == 'true'" in step["if"], step["if"]


def test_the_stale_tag_check_runs_before_the_build() -> None:
    """Order matters: a stale tag must be rejected, not silently rebuilt."""
    names = [s.get("name") or "" for s in _steps()]
    stale = next(i for i, n in enumerate(names) if n.startswith("Refuse a stale tag"))
    build = next(i for i, n in enumerate(names) if n.startswith("Build and push"))
    assert stale < build, names


def test_a_version_bump_is_what_triggers_a_publish() -> None:
    """The skip decision must key on the app version, not on ``force`` alone."""
    exists = _step("Is this version already published?")
    script = exists["run"]
    assert "needs.prepare.outputs.version" in script


def test_the_version_comes_from_addon_config() -> None:
    """The Supervisor pulls ``<image>:<version from addon/config.yaml>``."""
    prepare = _workflow()["jobs"]["prepare"]["steps"]
    version_step = next(
        s for s in prepare if (s.get("name") or "").startswith("Decide the version")
    )
    assert "manifest.json" in version_step["run"]


def test_the_guard_never_reads_the_label_at_the_top_level() -> None:
    """The fail-open defect: ``.config.Labels`` is absent on a manifest list.

    ``docker buildx imagetools inspect --format '{{json .Image}}'`` returns one
    entry per platform, so the extraction lives in ``image_revision.py`` and is
    tested offline in ``tests/unit/test_image_revision.py``.
    """
    script = _step("Refuse a stale tag")["run"]
    assert "image_revision.py" in script
    assert "'.config.Labels'" not in script


def test_the_panel_is_a_runtime_input_of_the_image() -> None:
    """A panel-only change must count as a change to the image.

    The ``identical`` shortcut skips the build when the pushed commit changes no
    file in ``runtime_paths``.  That list is a hand-maintained inventory of what
    the image actually ships.  A runtime input missing from it makes a change to
    that input look like "nothing to rebuild", so the tag keeps pointing at an
    image built from an older tree while the workflow reports success.
    """
    script = _step("Refuse a stale tag")["run"]
    match = re.search(r"runtime_paths=\((.*?)\)", script, re.DOTALL)
    assert match, "the stale-tag step no longer declares runtime_paths"
    paths = {line.strip() for line in match.group(1).splitlines() if line.strip()}
    assert "addon/ui" in paths, sorted(paths)


def test_the_panel_assets_exist_where_the_image_stages_them() -> None:
    """Every file the gateway serves must be staged into the image."""
    ui = ROOT / "addon" / "ui"
    for name in ("index.html", "app.css", "app.js", "mark.svg"):
        assert (ui / name).is_file(), name
