"""The retired CT vocabulary must not reappear in shipped product prose.

ADR-0034 removed the "dedicated device" CT concept in full: the channel role,
the monitoring level and the `DeviceTopology.dedicated_ct_ids` field, together
with the direct-measurement path they fed. A reader of the product
documentation who is told to assign a role, set a monitoring level, read an
attribution field or expect a private measurement must be told about things that
exist.

**The retired CT role is not `CircuitClass.DEDICATED`.** A circuit whose scope
holds exactly one device is a live, separately named concept: it ships in
`data/schemas/topology.json` and is returned by `GET /circuits`. Documentation
that describes circuit *classification* is therefore correct and keeps the word.
This gate is about the CT-channel-role vocabulary — `CTType.DEDICATED_DEVICE`,
`MonitoringLevel.DEDICATED`, `dedicated_ct_ids`, the declared
`dedicated_device` association and the private-measurement path keyed on them.

This is a **prose** gate, so it is deliberately about the *concept* rather than
the bare word. "Dedicated" is ordinary English and appears legitimately in
sentences that have nothing to do with CTs — "create a dedicated read-only HA
user", "a dedicated route", "the `dedicated` simulator stratum". A test that
failed on those would be satisfied only by mangling unrelated documentation,
which is the opposite of the point. The patterns below therefore match the ways
the retired role was actually expressed — enum member names, the removed field
and association, payload shapes that set the monitoring level to it, and the
measurement path that depended on them.

Two kinds of text are excluded, and the reason matters:

* **Historical record.** `docs/decisions/README.md` holds the accepted ADRs.
  ADR-0025 and ADR-0026 describe the concept as it stood when they were
  accepted, and ADR-0034 records the supersession. Editing those bodies would
  destroy the trail that explains why the role existed. `PROJECT_STATUS.md` and
  `RELEASE_NOTES.md` are append-only logs of what happened and when; a release
  that removes a concept must be able to name it.
* **The authorising specification.** `docs/specs/**` discusses the retired term
  throughout by construction — it is the plan for removing it.

Everything else under `docs/` is shipped product documentation and must be
clean. The list is built by scanning the tree rather than by naming files, so a
new document is covered the day it is added.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

#: Directories and files whose *purpose* is to discuss the retired concept or to
#: log what happened. See the module docstring for why each is exempt.
EXCLUDED_PARTS = (
    "docs/specs",  # the authorising specification
    "docs/decisions/README.md",  # accepted ADRs; bodies are historical record
    "PROJECT_STATUS.md",  # append-only engineering log
    "RELEASE_NOTES.md",  # append-only release log
)

#: The retired CT-role concept, as it was actually written. Each pattern names a
#: symbol, a payload value or a phrase a reader could act on. None of them match
#: ordinary English usage of the word, and none of them match
#: `CircuitClass.DEDICATED`, which is live.
#:
#: The monitoring-level patterns are scoped to the *key*, not to the value. A
#: bare code span ``dedicated`` is legitimate prose about a circuit class (the
#: `CircuitClass` table in `algorithms.md`, the `classification` field in the
#: `GET /circuits` example); only a monitoring key set to that value asserts the
#: retired level.
CONCEPT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bdedicated_device\b", "the removed CTType member"),
    (r"\bdedicated_ct_ids\b", "the removed DeviceTopology field"),
    (r"\bDEDICATED_DEVICE\b", "the removed enum constant"),
    (r"\bMonitoringLevel\.DEDICATED\b", "the removed enum member"),
    (
        r"[\"'`]?monitoring(?:_level)?[\"'`]?\s*[=:]\s*[\"'`]?dedicated[\"'`]?",
        "a payload or field that sets the removed monitoring level",
    ),
    (r"\bct_type\s*[=:]\s*[\"'`]?dedicated\b", "a topology payload using the removed role"),
    (
        r"\bmonitoring(?:\s+level)?\b[^.\n]{0,40}[\"'`]dedicated[\"'`]",
        "prose that offers the removed monitoring level",
    ),
    (r"\bdedicated\s+CT\b", "the retired measurement concept"),
    (r"\bdedicated\s+(?:device|appliance)s?\b", "the retired device concept"),
    (r"\bdirect_measured\b(?=[^.\n]{0,80}dedicated)", "the retired direct path"),
    (r"\bno_dedicated_ct\b", "the removed availability verdict"),
    (r"\bno_dedicated_device_ct\b", "the removed explanation reason"),
    (r"\bdedicated_ct_disagrees_with_state\b", "the removed explanation reason"),
)

CONCEPT_RE = [(re.compile(p, re.IGNORECASE), why) for p, why in CONCEPT_PATTERNS]


def shipped_docs() -> list[Path]:
    """Every markdown file under ``docs/`` that ships, plus the two root logs.

    Built by walking the tree so a new document is covered automatically. The
    root logs are included in the *inventory* (so the exclusion list is
    explicit) but excluded from the assertion below.
    """
    found = sorted(DOCS.rglob("*.md"))
    found += [ROOT / "PROJECT_STATUS.md", ROOT / "RELEASE_NOTES.md"]
    return found


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def is_excluded(relative: str) -> bool:
    return any(relative == part or relative.startswith(part + "/") for part in EXCLUDED_PARTS)


def offence(relative: str, text: str) -> list[str]:
    """Concept matches in one file, as ``path:line: pattern -> why`` strings.

    A region may opt out explicitly by wrapping itself in
    ``<!-- vocabulary-exempt: <reason> -->`` … ``<!-- /vocabulary-exempt -->``.
    This exists for **quoted historical text**, where editing the quotation
    would falsify the record: the product's original statement of purpose is
    preserved verbatim in ``WATTMOSAIC_MASTER_INSTRUCTIONS.md`` and necessarily
    names the concept. The exemption is textual, so it survives edits above it,
    and the reason is recorded in the document itself.
    """
    problems: list[str] = []
    exempt = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if "<!-- /vocabulary-exempt -->" in line:
            exempt = False
            continue
        if "<!-- vocabulary-exempt:" in line:
            exempt = True
            continue
        if exempt:
            continue
        for pattern, why in CONCEPT_RE:
            match = pattern.search(line)
            if match is not None:
                problems.append(f"{relative}:{lineno}: {match.group(0)!r} -> {why}")
    return problems


def test_the_inventory_covers_the_product_docs() -> None:
    """The scan must actually look at the documents it claims to guard.

    Without this, a broken glob would make every other assertion in the file
    vacuously true — the classic failure mode of a "no matches" test.
    """
    scanned = [_relative(p) for p in shipped_docs() if not is_excluded(_relative(p))]
    assert "docs/electrical_model.md" in scanned
    assert "docs/user_guide.md" in scanned
    assert "docs/integration.md" in scanned
    assert "docs/interface_contracts.md" in scanned
    assert "docs/configuration.md" in scanned
    assert "docs/api.md" in scanned
    assert "docs/algorithms.md" in scanned
    assert "docs/installation.md" in scanned
    assert "docs/testing.md" in scanned
    assert "docs/deployment.md" in scanned
    assert "docs/WATTMOSAIC_MASTER_INSTRUCTIONS.md" in scanned
    assert "docs/acceptance_report.md" in scanned
    assert "docs/implementation_plan_v2.md" in scanned
    assert "docs/limitations_v2.md" in scanned
    # And the excluded ones must be excluded, not silently absent.
    assert not any(path.startswith("docs/specs") for path in scanned)
    assert "docs/decisions/README.md" not in scanned


def test_no_shipped_doc_states_the_retired_concept() -> None:
    """AC-1: no product prose asserts a role, level or field that no longer exists."""
    problems: list[str] = []
    for path in shipped_docs():
        relative = _relative(path)
        if is_excluded(relative):
            continue
        problems += offence(relative, path.read_text(encoding="utf-8"))
    assert problems == [], (
        "the retired dedicated-device concept is asserted in shipped docs; "
        "ADR-0034 removed the role, the monitoring level and the field, so the "
        "prose must not offer them. Offences:\n  " + "\n  ".join(problems)
    )


def test_the_scan_is_not_vacuous() -> None:
    """Prove the patterns fire, so a passing suite means something.

    An empty pattern set, a typo in a regex, or an accidentally empty file list
    all make the real assertion pass for the wrong reason. Each offending
    phrase is fed to the same matcher the scan uses.
    """
    samples = [
        ("| Role | `main`, `branch`, `dedicated_device`, `unknown` |", "dedicated_device"),
        ('"monitoring": "dedicated",', '"dedicated"'),
        ("- `MonitoringLevel.DEDICATED` is removed", "MonitoringLevel.DEDICATED"),
        ("The device's `dedicated_ct_ids` list", "dedicated_ct_ids"),
        ("a dedicated CT measures this device alone", "dedicated CT"),
        ("Devices with dedicated CT measurements", "dedicated"),
    ]
    for line, expected in samples:
        found = offence("synthetic.md", line)
        assert found, f"no pattern matched {line!r} (expected {expected!r})"
        assert expected.lower() in found[0].lower(), (line, found)


def test_ordinary_english_is_not_flagged() -> None:
    """The scan is about the concept, not the word.

    These are real sentences from the shipped docs. Each uses "dedicated" in
    its ordinary sense and none describes a CT role, a monitoring level or an
    attribution field, so none may be reported.
    """
    allowed = [
        "`ha_token` is the least-privilege option: create a dedicated read-only HA user",
        "Prefer a dedicated read-only user token in `ha_token` when policy allows.",
        "`discover_placements` also has a dedicated route (`POST /discover/placements`,",
        "Implement a dedicated data-quality subsystem.",
    ]
    for line in allowed:
        assert offence("ordinary.md", line) == [], line


def test_the_exclusions_are_real_paths() -> None:
    """Every excluded path must exist.

    A renamed file would otherwise turn its exemption into a silent hole: the
    scan would skip a path that is no longer there while the real thing was
    reintroduced somewhere new.
    """
    missing = [part for part in EXCLUDED_PARTS if not (ROOT / part).exists()]
    assert missing == [], f"excluded path(s) do not exist: {missing}"


def test_adr_0034_records_the_supersession() -> None:
    """AC-11, as amended: the decision is present and cross-referenced.

    The specification called it "ADR-0027"; ADR-0027 through ADR-0033 were
    already accepted, so the record takes the next free id. This asserts the
    *intent* (the supersession is recorded and points at the records it
    supersedes), plus the numbering note that explains the deviation.
    """
    decisions = (ROOT / "docs" / "decisions" / "README.md").read_text(encoding="utf-8")
    assert "## ADR-0034" in decisions
    assert "Supersedes ADR-0025" in decisions
    # The body must own the numbering deviation rather than hide it.
    assert "Numbering note" in decisions
    assert "ADR-0027" in decisions.split("## ADR-0034", 1)[1]
    # And both superseded records must point forward at it.
    adr_0025 = decisions.split("## ADR-0025", 1)[1].split("## ADR-0026", 1)[0]
    adr_0026 = decisions.split("## ADR-0026", 1)[1].split("## ADR-0027", 1)[0]
    assert "ADR-0034" in adr_0025, "ADR-0025 has no supersession banner"
    assert "ADR-0034" in adr_0026, "ADR-0026 has no reference to ADR-0034"


@pytest.mark.parametrize("relative", ["docs/electrical_model.md", "docs/user_guide.md"])
def test_the_migrated_docs_are_clean(relative: str) -> None:
    """Spot-check the two documents that carried the most prose, by name.

    The tree-wide test above is the real gate; this makes a failure name the
    file a reader is most likely to be misled by, without relying on the walk.
    """
    assert offence(relative, (ROOT / relative).read_text(encoding="utf-8")) == []
