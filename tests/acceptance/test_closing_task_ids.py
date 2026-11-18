"""Meta-test: every AT file is open xfail-strict OR closed by a real task (K07/K07b).

Guards the acceptance contract itself:

* each ``test_atNN_*.py`` file is EITHER **open** — exactly one strict xfail
  marker ``reason="closed by <TASK>"`` — OR **closed** — zero xfail markers and
  a module-level constant ``CLOSED_BY = "<TASK>"``;
* in both cases the task id must exist in ``implementation-plan.md`` §3;
* there are exactly 20 files, one per AT number (AT-01 … AT-20);
* each file's task id and open/closed state agree with the per-file map in
  ``tests/acceptance/README.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN = REPO_ROOT / "docs/specs/realtime-topology-disaggregation/implementation-plan.md"
README = Path(__file__).resolve().parent / "README.md"

_XFAIL = re.compile(r'xfail\(strict=True, reason="closed by ([A-Z][0-9]+)"\)')
_CLOSED_BY = re.compile(r'^CLOSED_BY = "([A-Z][0-9]+)"', re.M)


def _plan_task_ids() -> set[str]:
    text = PLAN.read_text(encoding="utf-8")
    section = text.split("## 3. Task index", 1)[1].split("## 4.", 1)[0]
    return set(re.findall(r"^\| ([A-Z][0-9]+) \|", section, re.M))


def _at_files() -> dict[str, Path]:
    """Return ``{filename: path}`` for every ``test_atNN_*.py`` module."""
    here = Path(__file__).resolve().parent
    return {p.name: p for p in here.glob("test_at*.py")}


def _file_state(source: str) -> tuple[str, str]:
    """Return ``(state, task_id)`` for an acceptance module's source.

    ``state`` is ``"closed"`` (zero xfail markers + one ``CLOSED_BY``) or
    ``"open"`` (one strict xfail marker + zero ``CLOSED_BY``). Anything else —
    both forms at once, neither, or duplicates — is a contract violation.
    """
    xfails = _XFAIL.findall(source)
    closed_by = _CLOSED_BY.findall(source)
    if len(xfails) == 1 and not closed_by:
        return "open", xfails[0]
    if not xfails and len(closed_by) == 1:
        return "closed", closed_by[0]
    raise AssertionError(
        "expected exactly one strict xfail OR one CLOSED_BY, "
        f"got xfail={xfails} CLOSED_BY={closed_by}"
    )


def _readme_map() -> dict[str, tuple[str, str]]:
    """Parse README's file table into ``{filename: (task, state)}``."""
    result: dict[str, tuple[str, str]] = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not cells[0].startswith("`test_at"):
            continue
        name = cells[0].strip("`")
        task = cells[2]
        state = "closed" if "closed" in cells[3].lower() else "open"
        result[name] = (task, state)
    return result


def test_all_twenty_acceptance_files_exist() -> None:
    files = _at_files()
    assert len(files) == 20, f"expected exactly 20 AT files, got {len(files)}: {sorted(files)}"
    numbers = sorted(int(re.search(r"test_at(\d+)_", name).group(1)) for name in files)
    assert numbers == list(range(1, 21)), f"AT numbers must be 1..20, got {numbers}"


def test_open_or_closed_marker_names_an_existing_task_id() -> None:
    task_ids = _plan_task_ids()
    readme = _readme_map()
    for name, path in _at_files().items():
        state, task = _file_state(path.read_text(encoding="utf-8"))
        assert task in task_ids, f"{name}: task {task} is not a task id in §3"
        assert name in readme, f"README does not list {name}"
        expected_task, expected_state = readme[name]
        assert task == expected_task, f"{name}: task {task} != README task {expected_task}"
        assert state == expected_state, f"{name}: state {state} != README state {expected_state}"


def test_readme_lists_every_file_and_task() -> None:
    text = README.read_text(encoding="utf-8")
    for name in _at_files():
        assert name in text, f"README does not list {name}"
    for task, _state in _readme_map().values():
        assert re.search(rf"\b{re.escape(task)}\b", text), f"README does not list task {task}"
