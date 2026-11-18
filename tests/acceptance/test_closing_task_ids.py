"""Meta-test: acceptance markers name real closing tasks (K07).

Guards the xfail-strict contract itself:
* every ``test_atNN_*.py`` file's ``xfail(strict=True, reason="closed by <TASK>")``
  reason names an existing task id from ``implementation-plan.md`` §3;
* the 20 required files all exist and each names exactly one closing task;
* the ids agree with the per-file map in ``tests/acceptance/README.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN = REPO_ROOT / "docs/specs/realtime-topology-disaggregation/implementation-plan.md"
README = Path(__file__).resolve().parent / "README.md"

_FILE_TASK = {
    "test_at01_nested_no_double_count.py": "M1",
    "test_at02_parent_residual_not_containment.py": "T5",
    "test_at03_nonzero_standby_modeled.py": "L2",
    "test_at04_unknown_without_ha.py": "T6",
    "test_at05_response_delays_matched.py": "I2",
    "test_at06_joint_inference.py": "I3",
    "test_at07_group_not_fabricated.py": "I4",
    "test_at08_plug_anchor.py": "I3",
    "test_at09_covariance_nested.py": "I3",
    "test_at10_unknown_alternatives.py": "I1",
    "test_at11_cells_track_evidence.py": "M2",
    "test_at12_columns_multi_level.py": "U1",
    "test_at13_no_duplicate_secondary_columns.py": "M1",
    "test_at14_physical_load_counted_once.py": "I5",
    "test_at15_plug_relocation.py": "T7",
    "test_at16_reader_reconciliation.py": "T7",
    "test_at17_missing_not_zero.py": "D3",
    "test_at18_late_event_revision.py": "I6",
    "test_at19_replay_newer_model.py": "L7",
    "test_at20_ui_explains_estimates.py": "U2",
}


def _plan_task_ids() -> set[str]:
    text = PLAN.read_text(encoding="utf-8")
    section = text.split("## 3. Task index", 1)[1].split("## 4.", 1)[0]
    return set(re.findall(r"^\| ([A-Z][0-9]+) \|", section, re.M))


def test_all_twenty_acceptance_files_exist() -> None:
    here = Path(__file__).resolve().parent
    actual = {p.name for p in here.glob("test_at*.py")}
    assert actual == set(_FILE_TASK), (
        f"missing={set(_FILE_TASK) - actual} unexpected={actual - set(_FILE_TASK)}"
    )


def test_reason_names_an_existing_task_id() -> None:
    task_ids = _plan_task_ids()
    here = Path(__file__).resolve().parent
    for name, expected in _FILE_TASK.items():
        source = (here / name).read_text(encoding="utf-8")
        matches = re.findall(r'xfail\(strict=True, reason="closed by ([A-Z][0-9]+)"\)', source)
        assert len(matches) == 1, f"{name}: expected exactly one strict xfail reason"
        assert matches[0] == expected, f"{name}: reason {matches[0]} != expected {expected}"
        assert matches[0] in task_ids, f"{name}: reason {matches[0]} is not a task id in §3"


def test_readme_lists_every_file_and_task() -> None:
    text = README.read_text(encoding="utf-8")
    for name in _FILE_TASK:
        assert name in text, f"README does not list {name}"
    for task in set(_FILE_TASK.values()):
        assert re.search(rf"\b{re.escape(task)}\b", text), f"README does not list task {task}"
