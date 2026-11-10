"""The development-release pipeline, and the two ways it could silently break.

``main`` is now the development line: every push publishes a prerelease.  That
puts three things on the critical path, none of which a runtime assertion can
observe, so they are pinned here:

1. **The bump is committed.**  The Supervisor offers an App update only when
   ``addon/config.yaml``'s ``version:`` string changes.  A workflow that built
   an image without moving the version would publish something no one is
   offered -- the ``0.2.0-dev5`` defect.

2. **Bump, image, tag and Release are one workflow.**  A push made with the
   automatic ``GITHUB_TOKEN`` does not trigger other workflows, so splitting
   these into chained push-triggered workflows means the image is built from
   the *pre-bump* tree and the tag names a version no artifact carries.

3. **Development Releases are pruned.**  The Marketplace reads one unpaginated
   page of 30 Releases.  If prereleases fill it, the newest stable Release falls
   out, ``last_version`` becomes ``None``, and the installer falls back to
   ``default_branch`` -- which would offer a stable user the development line.
   That is worse than a stale version, so the prune is mandatory.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yaml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _jobs() -> dict:
    return _workflow()["jobs"]


def _steps(job: str) -> list[dict]:
    return _jobs()[job].get("steps", [])


def _step(job: str, prefix: str) -> dict:
    for step in _steps(job):
        if (step.get("name") or "").startswith(prefix):
            return step
    raise AssertionError(f"{job} has no step named {prefix!r}")


class TestTriggers:
    def test_main_is_a_release_branch(self) -> None:
        """``main`` is the development line, so a push there publishes."""
        triggers = _workflow()[True] if True in _workflow() else _workflow()["on"]
        assert "main" in triggers["push"]["branches"]

    def test_the_old_dev_branch_is_gone(self) -> None:
        """Keeping a second long-lived branch is the fork this rework removes."""
        triggers = _workflow()[True] if True in _workflow() else _workflow()["on"]
        assert "dev" not in triggers["push"]["branches"]

    def test_stable_is_a_release_branch(self) -> None:
        triggers = _workflow()[True] if True in _workflow() else _workflow()["on"]
        assert "stable" in triggers["push"]["branches"]


class TestSinglePipeline:
    def test_image_and_release_are_jobs_in_this_workflow(self) -> None:
        """A bot push does not cascade, so everything must be in one run."""
        assert {"prepare", "image", "publish"} <= set(_jobs())

    def test_the_image_is_built_after_the_bump(self) -> None:
        """Building from the pre-bump tree would tag the wrong code."""
        assert "prepare" in _jobs()["image"]["needs"]

    def test_the_release_is_published_after_the_image(self) -> None:
        """A Release claims the version is installable; the image must exist."""
        needs = _jobs()["publish"]["needs"]
        assert "image" in needs

    def test_the_release_checks_out_the_bumped_commit(self) -> None:
        """Checking out the triggering SHA would tag the pre-bump tree."""
        checkout = _steps("publish")[0]
        assert checkout["with"]["ref"] == "${{ needs.prepare.outputs.sha }}"

    def test_the_image_checks_out_the_bumped_commit(self) -> None:
        checkout = _steps("image")[0]
        assert checkout["with"]["ref"] == "${{ needs.prepare.outputs.sha }}"


class TestVersionBump:
    def test_the_bump_is_committed_and_pushed(self) -> None:
        """Without the commit the App is never offered the new version."""
        script = _step("prepare", "Commit the bump")["run"]
        assert "git commit" in script
        assert "git push" in script

    def test_the_bump_commit_is_marked(self) -> None:
        """The marker is what stops a PAT-configured repo recursing forever."""
        script = _step("prepare", "Commit the bump")["run"]
        assert "[wm-auto-version]" in script

    def test_the_guard_checks_for_the_marker(self) -> None:
        script = _step("prepare", "Never re-enter")["run"]
        assert "[wm-auto-version]" in script
        assert "skip=true" in script

    def test_an_already_tagged_version_is_incremented(self) -> None:
        """A push that reuses a published version must not reuse its tag."""
        script = _step("prepare", "Decide the version")["run"]
        assert "bump_version.py --next" in script
        assert "refs/tags/v${current}" in script

    def test_stable_does_not_auto_increment(self) -> None:
        """A stable release is a human decision, not a counter."""
        script = _step("prepare", "Decide the version")["run"]
        stable_branch = script.split('== "stable"')[1].split("elif")[0]
        assert "bump_version.py --next" not in stable_branch

    def test_the_channel_check_runs_after_the_bump(self) -> None:
        """It must judge the version being published, not the one pushed."""
        names = [s.get("name") or "" for s in _steps("prepare")]
        write = next(i for i, n in enumerate(names) if n.startswith("Write the version"))
        check = next(
            i for i, n in enumerate(names) if n.startswith("The tree must describe")
        )
        assert write < check, names

    def test_notes_are_generated_for_an_automatic_bump(self) -> None:
        """The contract refuses to tag a version the notes do not document."""
        script = _step("prepare", "Write the version")["run"]
        assert "--notes-file" in script
        assert "git log" in script


class TestPruning:
    def test_pruning_runs(self) -> None:
        assert "prune" in _jobs()

    def test_pruning_only_affects_prereleases(self) -> None:
        """Stable Releases record what users were offered; never delete them."""
        script = _steps("prune")[0]["run"]
        assert "select(.isPrerelease)" in script

    def test_pruning_deletes_the_git_tag_too(self) -> None:
        """A pruned version must not stay installable by tag name."""
        script = _steps("prune")[0]["run"]
        assert "--cleanup-tag" in script

    def test_pruning_is_skipped_for_stable_releases(self) -> None:
        assert "prerelease == 'true'" in _jobs()["prune"]["if"]

    def test_the_retained_count_leaves_room_for_stable(self) -> None:
        """The Marketplace window is 30; filling it with dev builds is the bug."""
        text = WORKFLOW.read_text()
        match = re.search(r"KEEP_PRERELEASES:\s*\"?(\d+)\"?", text)
        assert match, "KEEP_PRERELEASES is not declared"
        keep = int(match.group(1))
        assert keep < 30, keep

    def test_the_workflow_refuses_an_unsafe_retained_count(self) -> None:
        """A future edit to `keep` must fail loudly, not silently break stable."""
        script = _steps("prune")[0]["run"]
        assert "keep >= window" in script
        assert "exit 1" in script

    def test_the_window_size_is_documented(self) -> None:
        """The number 30 is a fact about the Marketplace, not a guess."""
        text = WORKFLOW.read_text()
        assert "per_page: 30" in text
        assert "default_branch" in text


class TestSafety:
    def test_a_prerelease_is_never_published_as_latest(self) -> None:
        script = _step("publish", "Publish the Release")["run"]
        assert "--prerelease --latest=false" in script

    def test_a_stable_release_is_published_as_latest(self) -> None:
        script = _step("publish", "Publish the Release")["run"]
        assert "--latest" in script

    def test_the_release_waits_for_a_pullable_image(self) -> None:
        script = _step("publish", "The image for this version must be pullable")["run"]
        assert "imagetools inspect" in script
        assert "exit 1" in script
