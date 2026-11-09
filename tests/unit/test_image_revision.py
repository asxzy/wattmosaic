"""Tests for the published-image revision reader.

The workflow uses this to decide whether a push has already been published.
It previously parsed the label with an inline one-liner that read
``.config.Labels`` at the top level of the ``imagetools`` JSON — but that JSON
is keyed by platform on a multi-arch manifest list, so the read always
returned nothing.  The guard then took its "already published from this
commit" branch unconditionally and reported success for an image built from a
different commit.

These tests pin both shapes so that regression cannot pass silently.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "dev" / "image_revision.py"

REVISION = "6c930d8850277198fe7563fcab52675e8f4512f4"
OTHER = "0badc0ffee0ddf00d0badc0ffee0ddf00d0badc"


def _load():
    spec = importlib.util.spec_from_file_location("wm_image_revision", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["wm_image_revision"] = module
    spec.loader.exec_module(module)
    return module


def _multiarch(amd: str | None = REVISION, arm: str | None = REVISION) -> dict:
    """The shape ``imagetools`` returns for a manifest list."""

    def entry(revision: str | None) -> dict:
        labels = {}
        if revision is not None:
            labels["org.opencontainers.image.revision"] = revision
        return {"created": "2026-11-09T19:54:37Z", "config": {"Labels": labels}}

    return {"linux/amd64": entry(amd), "linux/arm64": entry(arm)}


def test_a_multiarch_manifest_list_yields_the_revision():
    """The shape that broke the original one-liner."""
    module = _load()
    assert module.revisions(_multiarch()) == {REVISION}


def test_a_flat_single_platform_object_yields_the_revision():
    module = _load()
    flat = {"config": {"Labels": {"org.opencontainers.image.revision": REVISION}}}
    assert module.revisions(flat) == {REVISION}


def test_both_architectures_agree_is_not_an_error():
    module = _load()
    data = _multiarch(REVISION, REVISION)
    assert len(module.revisions(data)) == 1


def test_disagreeing_architectures_are_rejected():
    """An inconsistent manifest list must not silently pick one."""
    module = _load()
    assert module.revisions(_multiarch(REVISION, OTHER)) == {REVISION, OTHER}


def test_a_missing_label_is_empty_not_a_guess():
    module = _load()
    assert module.revisions(_multiarch(None, None)) == set()


def test_the_top_level_label_read_that_failed_is_empty():
    """Pin the defect: the old accessor sees nothing on a manifest list.

    This is the assertion that would have caught it.  ``data["config"]`` does
    not exist when the response is platform-keyed.
    """
    data = _multiarch()
    assert (data.get("config") or {}).get("Labels") is None


def _run(payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_cli_prints_one_revision():
    result = _run(json.dumps(_multiarch()))
    assert result.returncode == 0
    assert result.stdout.strip() == REVISION


def test_the_cli_prints_empty_for_an_unlabelled_image():
    result = _run(json.dumps(_multiarch(None, None)))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_the_cli_rejects_inconsistent_revisions():
    result = _run(json.dumps(_multiarch(REVISION, OTHER)))
    assert result.returncode == 1
    assert "inconsistent" in result.stderr


def test_the_cli_rejects_malformed_json():
    result = _run("not json")
    assert result.returncode == 2
    assert "not valid JSON" in result.stderr


def test_the_cli_rejects_an_unexpected_shape():
    result = _run(json.dumps({"unexpected": "shape"}))
    assert result.returncode == 2
    assert "no image entries" in result.stderr


def test_an_empty_object_is_rejected_rather_than_printed():
    """A manifest list with no entries means the read failed; do not print ''."""
    result = _run("{}")
    assert result.returncode == 2


def test_the_script_exists_and_is_executable_by_python():
    assert SCRIPT.is_file()
    source = SCRIPT.read_text()
    assert "raise SystemExit(main())" in source
