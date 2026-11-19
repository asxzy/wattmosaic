"""Every module under ``tools/`` must import cleanly.

Z2 deleted engine modules that several developer/audit tools still imported,
and the breakage was invisible in CI because the tools are invoked as scripts
rather than imported by the test suite. This gate imports every ``tools/**/*.py``
(importlib) so a future engine deletion cannot silently rot a tool again.

Modules that legitimately cannot be imported at collection time (e.g. a script
that needs a third-party CLI dependency at import) are loaded with their
directory on ``sys.path`` -- the same context a direct script run gets -- so the
gate measures real script importability, not a packaged-import convention.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
ENGINE = ROOT / "engine"


def _tool_modules() -> list[Path]:
    return sorted(p for p in TOOLS.rglob("*.py") if "__pycache__" not in p.parts)


def test_every_tools_module_imports() -> None:
    """No tool may import a deleted engine module (or otherwise fail to import)."""
    # The tools resolve the worktree's engine; mirror their own sys.path setup.
    for path in (str(ENGINE), str(ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)

    failures: list[str] = []
    for path in _tool_modules():
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        name = "tools_import_gate." + ".".join(parts)
        parent = str(path.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None, f"no import spec for {path}"
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - any import failure is a gate failure
            failures.append(f"{path.relative_to(ROOT)}: {type(exc).__name__}: {exc}")

    assert not failures, "tools failed to import:\n" + "\n".join(failures)


def test_tools_gate_covers_the_ported_tools() -> None:
    """The gate must actually include the ported Z2c tools."""
    names = {p.relative_to(ROOT).as_posix() for p in _tool_modules()}
    assert "tools/audit/replay_capture.py" in names
    assert "tools/audit/derive_state_entities.py" in names
