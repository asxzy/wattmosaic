"""The installed engine must carry every runtime data file.

The add-on image installs the engine with ``pip install .`` (a setuptools
wheel build), not from the source tree the rest of the suite imports.  A data
file missing from the wheel therefore passes every other test and only fails
in production: v0.3.0-dev.4 crashed at import because
``inference/evidence_weights.json`` was not packaged.

This test builds the real wheel and asserts that every non-Python file under
``engine/wattmosaic_engine`` is inside it.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "engine" / "wattmosaic_engine"


def _package_data_files() -> set[str]:
    files: set[str] = set()
    for path in PACKAGE.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix in {".py", ".pyc"}:
            continue
        files.add(path.relative_to(PACKAGE.parent).as_posix())
    return files


@pytest.mark.skipif(
    importlib.util.find_spec("setuptools") is None,
    reason="setuptools is required to build the wheel offline",
)
def test_wheel_ships_every_package_data_file(tmp_path: Path) -> None:
    expected = _package_data_files()
    assert "wattmosaic_engine/inference/evidence_weights.json" in expected

    # Build from a clean copy of exactly what the Dockerfile copies
    # (pyproject.toml, README.md, engine/).  Building in place would reuse a
    # local editable install's *.egg-info/SOURCES.txt, which already lists the
    # data files and hides the packaging defect.
    src = tmp_path / "src"
    src.mkdir()
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(REPO / name, src / name)
    shutil.copytree(
        REPO / "engine",
        src / "engine",
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build"),
    )
    out = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel", "--quiet", "--no-deps",
            "--no-build-isolation", "--wheel-dir", str(out), str(src),
        ],
        check=True,
        cwd=tmp_path,
    )
    wheels = list(out.glob("wattmosaic_engine-*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as wheel:
        shipped = set(wheel.namelist())

    missing = sorted(expected - shipped)
    assert not missing, f"package data missing from the wheel: {missing}"
