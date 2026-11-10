"""The dev tools must analyze the checkout they live in, not an installed copy.

A git worktree shares its parent's virtualenv.  When the package is installed
editable (``pip install -e .``), that virtualenv's ``.pth`` points at the *main*
checkout, so an unqualified ``import wattmosaic_engine`` resolves to a different
tree than the one being edited.  Two gates were silently reading the wrong code
because of this:

* ``tools/dev/export_schemas.py`` regenerated the JSON schemas from the main
  checkout, so a contract change in a worktree produced "schemas are current"
  while ``data/schemas`` still described the other tree.
* ``tools/dev/verify.py`` ran each gate as a subprocess with the inherited
  environment, so ``--check-config`` reported the main checkout's
  ``engine_version`` in the recorded evidence.

Both now pin ``engine/`` onto ``sys.path`` / ``PYTHONPATH`` ahead of anything the
environment supplies.  These tests fail if that is removed.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_export_schemas_prefers_this_checkout() -> None:
    module = _load("wm_export_schemas", ROOT / "tools" / "dev" / "export_schemas.py")
    assert module.ENGINE_ROOT == ROOT / "engine"
    assert str(module.ENGINE_ROOT) in sys.path
    # The import must resolve to this tree, which is the whole point.
    import wattmosaic_engine

    assert Path(wattmosaic_engine.__file__).resolve().is_relative_to(ROOT)


def test_export_schemas_engine_root_is_first_on_the_path() -> None:
    """A stale editable install must not shadow the worktree's engine."""
    module = _load("wm_export_schemas_order", ROOT / "tools" / "dev" / "export_schemas.py")
    engine_index = sys.path.index(str(module.ENGINE_ROOT))
    for entry in sys.path[:engine_index]:
        # Earlier entries may be pytest's own roots; none may be a different
        # checkout that also contains a wattmosaic_engine package.
        candidate = Path(entry) / "wattmosaic_engine"
        assert not candidate.is_dir(), f"{entry} shadows the worktree engine"


def test_verify_pins_the_engine_on_the_subprocess_path(tmp_path: Path) -> None:
    module = _load("wm_verify", ROOT / "tools" / "dev" / "verify.py")
    captured: dict[str, object] = {}

    real_run = module.subprocess.run

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    module.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        module._run(module.Gate("probe", ["python", "-c", "pass"]))
    finally:
        module.subprocess.run = real_run  # type: ignore[assignment]

    env = captured["env"]
    assert isinstance(env, dict)
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(ROOT / "engine")
    assert captured["cwd"] == ROOT


def test_verify_preserves_an_existing_pythonpath() -> None:
    module = _load("wm_verify_keep", ROOT / "tools" / "dev" / "verify.py")
    captured: dict[str, object] = {}
    real_run = module.subprocess.run
    real_environ = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = "/pre/existing"

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        captured["env"] = kwargs.get("env")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    module.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        module._run(module.Gate("probe", ["python", "-c", "pass"]))
    finally:
        module.subprocess.run = real_run  # type: ignore[assignment]
        if real_environ is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = real_environ

    parts = captured["env"]["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(ROOT / "engine")
    assert "/pre/existing" in parts


def test_scrubber_removes_absolute_checkout_paths() -> None:
    """Tracked evidence must not name the operator's filesystem.

    The unit-test warning output contains an absolute path to this worktree, so
    the first regeneration of ``data/verification.json`` published it.  The
    scrubber keeps the file name (it is diagnostic) and drops the directory.
    """
    module = _load("wm_verify_scrub", ROOT / "tools" / "dev" / "verify.py")
    scrubbed = module._scrub(
        "/Users/someone/src/wattmosaic-ui/addon/gateway.py:354: warning"
    )
    assert "/Users/" not in scrubbed
    assert "someone" not in scrubbed
    assert scrubbed.endswith("gateway.py:354: warning")
    assert "<redacted-path>" in scrubbed
    # A home-directory checkout is the same hazard on Linux.
    assert "/home/" not in module._scrub("/home/ci/repo/tools/x.py")
