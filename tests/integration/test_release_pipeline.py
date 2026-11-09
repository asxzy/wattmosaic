"""Guard the release pipeline against publishing a stale, mismatched tag.

Why this file exists
--------------------
``0.2.0-dev5`` was published from commit ``7a0922d``.  A later commit
``69ebbfb`` fixed two accounting defects and pushed to ``dev`` **without
bumping** ``version:`` in ``addon/config.yaml``.  The image workflow ran, saw
that the tag already existed, published nothing, and still reported success.
The live app therefore kept running the pre-fix code while the HA update entity
advertised ``0.2.0-dev5`` -- a silent "the fix shipped" lie.

The workflow now stamps ``org.opencontainers.image.revision`` into every image
and refuses to treat an existing tag as up to date unless that label matches the
current commit.  These tests pin the workflow contract, because the failure mode
is an *absence* of work that no runtime assertion can observe.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "app-image.yaml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps() -> list[dict]:
    return _workflow()["jobs"]["publish"]["steps"]


def _step(prefix: str) -> dict:
    for step in _steps():
        if (step.get("name") or "").startswith(prefix):
            return step
    raise AssertionError(f"workflow has no step named {prefix!r}")


def test_image_is_stamped_with_the_building_commit() -> None:
    """Without a revision label the stale-tag check has nothing to compare."""
    build = _step("Build and push")
    labels = build["with"]["labels"]
    assert "org.opencontainers.image.revision=${{ github.sha }}" in labels, labels


def test_existing_tag_is_checked_against_this_commit() -> None:
    """An existing tag must be verified, not assumed to contain this commit."""
    step = _step("Refuse a stale tag")
    script = step["run"]
    assert "org.opencontainers.image.revision" in script, script
    assert "github.sha" in script, script
    # A mismatch must fail the job: exiting 0 here is the original defect.
    assert re.search(r"exit\s+1", script), script
    # It must only run when the tag already exists (the build path is untouched).
    assert "exists.outputs.exists == 'true'" in step["if"], step["if"]


def test_the_stale_tag_check_runs_before_the_skip_notice() -> None:
    """Order matters: the notice must not be the only thing a stale push does."""
    names = [s.get("name") or "" for s in _steps()]
    stale = next(i for i, n in enumerate(names) if n.startswith("Refuse a stale tag"))
    notice = next(i for i, n in enumerate(names) if n.startswith("Explain why nothing"))
    assert stale < notice, names


def test_a_version_bump_is_what_triggers_a_publish() -> None:
    """The skip decision must key on the app version, not on ``force`` alone."""
    exists = _step("Is this version already published?")
    script = exists["run"]
    assert "addon/config.yaml" in _workflow()["jobs"]["publish"]["steps"][1]["run"]
    assert "steps.version.outputs.version" in script
