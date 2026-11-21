"""Data-rendering tests for the panel's derived backend-data cards (B4-UIa).

The panel is shipped as flat native ES modules under ``addon/ui``.  These tests
import the real modules under node with a DOM shim on ``globalThis`` (see
``panel_harness.py``) and assert on the text the browser would render for sample
API payloads.  No source slicing or module-name markers: that was the old
single-IIFE harness, removed by the ES-module port.
"""

from __future__ import annotations

import json

import pytest

from tests.unit.panel_harness import DOM_SHIM, EVENT_DOM_SHIM, lit, node_path, run_esm

SAMPLE_RESIDUALS = {
    "window_hours": 24.0,
    "count": 2,
    "components": [
        {
            "timestamp": "2026-11-17T10:00:00+00:00",
            "residual_w": 800.0,
            "baseline_w": 300.0,
            "dynamic_w": 500.0,
            "noise_w": 12.5,
            "baseline_estimator": "median_of_window",
            "resolved": False,
            "kind": "positive",
        },
        {
            "timestamp": "2026-11-17T09:00:00+00:00",
            "residual_w": -125.0,
            "baseline_w": -125.0,
            "dynamic_w": 0.0,
            "noise_w": None,
            "baseline_estimator": "no_window_current_only",
            "resolved": False,
            "kind": "negative",
        },
    ],
}

SAMPLE_MAPPING = {
    "reader_configuration_version": "cfg-2026-11-17",
    "topology_version": "v7",
    # Additive device-major axes from the backend: real devices are rows, CT
    # channels are columns.  CT ids must never appear in the devices axis.
    "devices": ["light.kitchen", "light.hall"],
    "readers": ["ct_kitchen", "ct_shared"],
    "cells": [
        {
            "device_id": "light.kitchen",
            "reader_id": "ct_kitchen",
            "status": "confirmed",
            "observability": "direct",
            "estimated_contribution_w": 45.0,
            "min_contribution_w": 40.0,
            "max_contribution_w": 50.0,
            "relation_type": "one_to_one",
            "is_unknown_load": False,
            # A ConfidenceVector: separate dimensions, never a blended score.
            "confidence": {
                "power": 0.91,
                "ct_association": 0.95,
                "topology": None,
                "circuit_classification": None,
                "residual": None,
                "method": "blend_v1",
            },
            "evidence_count": 12,
            "sources_used": [
                {"source": "ct_measurement", "reason": "rows_in_fit:ct_kitchen"},
                {"source": "topology", "reason": "graph_used_for_rows_and_identifiability"},
            ],
            "model_version": 3,
            "topology_version": "v7",
        },
        {
            "device_id": "light.hall",
            "reader_id": "ct_shared",
            "status": "non_identifiable",
            "observability": "indirect",
            "estimated_contribution_w": None,
            "min_contribution_w": 10.0,
            "max_contribution_w": 90.0,
            "relation_type": "many_to_one",
            "is_unknown_load": False,
            "confidence": {
                "power": None,
                "ct_association": 0.4,
                "topology": None,
                "circuit_classification": None,
                "residual": None,
                "method": "unavailable",
            },
            "evidence_count": 5,
            "sources_used": [
                {"source": "topology", "reason": "graph_used_for_rows_and_identifiability"},
            ],
            "model_version": None,
            "topology_version": "v7",
        },
    ],
    "coverage": {
        "counts": {
            "cell_count": 2,
            "by_status": {"confirmed": 1, "non_identifiable": 1},
            "by_cardinality": {"one_to_one": 1, "many_to_one": 1},
            "unknown_load_count": 0,
        },
        "unobserved_devices": [],
        "unexplained_scopes": [],
        "readers_without_attributable_device": [],
        "non_summing_readers": [],
    },
}

SAMPLE_DEVICES = [
    {
        "device_id": "light.hall",
        "name": "Hall lights",
        "latest_result": {
            "power_w": 0.0,  # 0.0 placeholder — NOT a per-device measurement
            "min_w": 30.0,
            "max_w": 90.0,
            "group_members": ["light.hall", "light.entry"],
            "source": "inferred",
            # The joint-inference ledger the engine attaches to every result.
            "sources_used": [
                {"source": "topology", "reason": "graph_used_for_rows_and_identifiability"},
                {"source": "ct_measurement", "reason": "rows_in_fit:ct_kitchen"},
            ],
            "sources_unused": [
                {"source": "learned_models", "reason": "no_model_for_device"},
                {"source": "ha_attributes", "reason": "no_device_level_attributes"},
            ],
        },
    },
    {
        "device_id": "light.entry",
        "name": "Entry lights",
        "latest_result": {
            "power_w": 0.0,
            "min_w": 30.0,
            "max_w": 90.0,
            "group_members": ["light.hall", "light.entry"],
            "source": "inferred",
            "sources_used": [
                {"source": "topology", "reason": "graph_used_for_rows_and_identifiability"},
                {"source": "ct_measurement", "reason": "rows_in_fit:ct_kitchen"},
            ],
            "sources_unused": [
                {"source": "learned_models", "reason": "no_model_for_device"},
                {"source": "ha_attributes", "reason": "no_device_level_attributes"},
            ],
        },
    },
]

SAMPLE_TOPOLOGY = {
    "version": 7,
    "status": "active",
    "created_at": "2026-11-16T10:00:00+00:00",
    "edge_count": 12,
    "created_by": "op",
    "summary": "rebalanced",
}

SAMPLE_MODEL = {
    "device_id": "light.kitchen",
    "active": {"version": 3},
    "versions": [
        {
            "model_type": "linear",
            "version": 3,
            "status": "confirmed",
            "confidence": 0.91,
            "created_at": "2026-11-15T10:00:00+00:00",
        },
    ],
}

SAMPLE_VERSION = {
    "versions": [
        {
            "version_id": "v6",
            "version_number": 6,
            "status": "frozen",
            "edge_count": 11,
            "created_at": "2026-11-14T10:00:00+00:00",
            "created_by": "sys",
            "summary": "initial",
        },
    ],
}

SAMPLE_STATUS = {
    "audit": {
        "last_pass": {
            "configuration_changes": [
                {
                    "kind": "reader_reassign",
                    "status": "suspected",
                    "confidence": 0.62,
                    "evidence_event_ids": ["e1", "e2"],
                    "occurred_at": "2026-11-17T08:00:00+00:00",
                    "device_id": "light.hall",
                    "reader_id": "ct_shared",
                },
            ],
        },
    },
}

SAMPLE_DIAGNOSTICS = {
    "errors": {
        "recent_system_events": [
            {
                "category": "configuration_change",
                "timestamp": "2026-11-17T08:00:00+00:00",
                "details": {
                    "event": {
                        "kind": "threshold_tune",
                        "status": "suspected",
                        "confidence": 0.44,
                        "evidence_event_ids": ["e3"],
                    },
                },
            },
            {
                "category": "quality",
                "timestamp": "2026-11-17T07:30:00+00:00",
                "level": "degraded",
                "message": "signal dropped below threshold",
            },
        ],
        "recent_quality_events": [
            {
                "timestamp": "2026-11-17T07:00:00+00:00",
                "code": "noise_spike",
                "severity": "warn",
                "detail": "noise floor rose",
            },
        ],
    },
}

SAMPLE_ISOLATION = {
    "verdicts": [
        {
            "device_id": "light.hall",
            "status": "isolated",
            "occurred_at": "2026-11-17T06:00:00+00:00",
            "reason": "contaminated_window",
            "competing_device_ids": ["light.entry", "ct_shared"],
            "power_window_contaminated": True,
            "window_start": "2026-11-17T05:00:00+00:00",
            "window_end": "2026-11-17T06:00:00+00:00",
        },
    ],
}

SAMPLE_SNAPSHOT = {
    "timestamp": "2026-11-17T10:00:00+00:00",
    "results": [
        d["latest_result"] for d in SAMPLE_DEVICES if d.get("latest_result")
    ],
}

SAMPLE_RESET = {
    "reset": True,
    "backup_path": "/data/wattmosaic.reset-backup.2026-11-17T10-00-00.db",
    "schema_version": 7,
    "latest_schema_version": 7,
}

_STALE_GATING_STRINGS = (
    "Learning is paused",
    "confirm the topology to start",
    "being rejected as ambiguous",
    "until the topology is confirmed",
    "penalty until",
)

_DYNAMIC_TOPOLOGY_NOTE = (
    "Topology is dynamic (candidate); it is authoritative for inference and "
    "learning and carries a reduced confidence (candidate 0.6 vs confirmed "
    "1.0). No confirmation is required."
)

SAMPLE_DYNAMIC_STATUS = {
    "learning": {
        "active": True,
        "effective_state": "learning",
        "topology_note": _DYNAMIC_TOPOLOGY_NOTE,
        "remedy": None,
    },
}

SAMPLE_CANDIDATE_TOPOLOGY = {
    "version": 7,
    "status": "candidate",
    "channels": [
        {"ct_type": "main", "parent_ct_id": None},
        {"ct_type": "branch", "parent_ct_id": "ct_main"},
    ],
    "devices": [{"device_id": "light.kitchen"}, {"device_id": "light.hall"}],
    "created_at": "2026-11-16T10:00:00+00:00",
}

def _render(payloads: dict) -> dict[str, str]:
    """Render the three data cards from the real modules under node."""
    if not node_path():
        pytest.skip("node is not installed; the panel does not need it at runtime")
    program = (
        "import { residualDecompositionCard, mappingMatrixCard, groupRangesCard } from './view-devices.js';\n"
        f"const SAMPLE_RESIDUALS = {lit(payloads['residuals'])};\n"
        f"const SAMPLE_MAPPING = {lit(payloads['mapping'])};\n"
        f"const SAMPLE_DEVICES = {lit(payloads['devices'])};\n"
        "const groups = groupRangesCard(SAMPLE_DEVICES);\n"
        "console.log(JSON.stringify({\n"
        "  residual: residualDecompositionCard(SAMPLE_RESIDUALS).textContent,\n"
        "  mapping: mappingMatrixCard(SAMPLE_MAPPING).textContent,\n"
        "  groups: groups ? groups.textContent : '',\n"
        "}));\n"
    )
    return run_esm(program, shim=DOM_SHIM)

@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    return _render(
        {"residuals": SAMPLE_RESIDUALS, "mapping": SAMPLE_MAPPING, "devices": SAMPLE_DEVICES}
    )


def test_panel_renders_residual_decomposition(rendered: dict[str, str]) -> None:
    """The residual card shows the three-way split, estimator and resolved state."""
    text = rendered["residual"]
    assert "Residual decomposition" in text
    # Signed residual plus each published component and its unit.
    for value in ("800 W", "300 W", "500 W", "12.5 W", "125 W"):
        assert value in text, f"missing residual field value {value!r}"
    # The estimator and the resolved state, not just the scalar unknown_residual.
    assert "median_of_window" in text
    assert "no_window_current_only" in text
    assert "Unresolved" in text
    assert "Kind" in text  # the table exposes the residual's signed kind column


def test_panel_renders_mapping_matrix(rendered: dict[str, str]) -> None:
    """The mapping card shows cells with status, observability and bounds."""
    text = rendered["mapping"]
    assert "Device-reader mapping" in text
    for value in (
        "light.kitchen",
        "ct_kitchen",
        "Confirmed",
        "Direct",
        # The panel's shared watts() formatter keeps one decimal for 10..100 W,
        # so a faithful render of 45.0/40.0/50.0/90.0 is "45.0 W" etc.
        "45.0 W",
        "40.0 W",
        "50.0 W",
        "One-to-one",
        "Non-identifiable",
        "Indirect",
        "Many-to-one",
        "90.0 W",
    ):
        assert value in text, f"missing mapping field value {value!r}"
    assert "Coverage" in text


def test_panel_renders_group_ranges(rendered: dict[str, str]) -> None:
    """Group ranges show min_w/max_w with a NON-IDENTIFIABLE label and members."""
    text = rendered["groups"]
    assert "Identifiability ranges" in text
    assert "NON-IDENTIFIABLE" in text
    assert "30.0 W" in text and "90.0 W" in text  # min_w / max_w
    assert "Group total of:" in text
    assert "light.hall" in text and "light.entry" in text  # group_members


def test_non_identifiable_device_has_no_fabricated_watt(rendered: dict[str, str]) -> None:
    """A non-identifiable device shows a group range, never a per-device watt.

    Both sample devices carry ``power_w: 0.0`` — a placeholder the engine writes
    for inseparable groups.  Rendering that value would fabricate a measurement
    ("0.00 W"), so the groups card must show only the group range and label.
    """
    text = rendered["groups"]
    assert "NON-IDENTIFIABLE" in text
    assert "30.0 W" in text and "90.0 W" in text
    # The 0.0 power_w placeholder must never surface as a fabricated watt, and
    # the card must not present a per-device "Estimated power" figure.
    assert "0.00 W" not in text
    assert "Estimated power" not in text


# ---------------------------------------------------------------------------
# B4-UIb: version history, suspected changes and event evidence.
#
# The three new renderers live in the same harness slice as the UIa cards (all
# before ``systemCard``), so the very same DOM shim and slice are reused.  The
# payload shapes are taken from the engine wire forms:
#   version  -> {versions:[{version_id, version_number, status, ...}]}
#   model    -> /models/{id} {device_id, active:{version}, versions:[...]}
#   status   -> /status {audit:{last_pass:{configuration_changes:[...]}}}
#   diagnostics -> {errors:{recent_system_events, recent_quality_events}}
#   isolation -> {verdicts:[{device_id, status, reason, ...}]}
# ---------------------------------------------------------------------------

SAMPLE_TOPOLOGY = {
    "version": 7,
    "status": "active",
    "created_at": "2026-11-16T10:00:00+00:00",
    "edge_count": 12,
    "created_by": "op",
    "summary": "rebalanced",
}

SAMPLE_MODEL = {
    "device_id": "light.kitchen",
    "active": {"version": 3},
    "versions": [
        {
            "model_type": "linear",
            "version": 3,
            "status": "confirmed",
            "confidence": 0.91,
            "created_at": "2026-11-15T10:00:00+00:00",
        },
    ],
}

SAMPLE_VERSION = {
    "versions": [
        {
            "version_id": "v6",
            "version_number": 6,
            "status": "frozen",
            "edge_count": 11,
            "created_at": "2026-11-14T10:00:00+00:00",
            "created_by": "sys",
            "summary": "initial",
        },
    ],
}

SAMPLE_STATUS = {
    "audit": {
        "last_pass": {
            "configuration_changes": [
                {
                    "kind": "reader_reassign",
                    "status": "suspected",
                    "confidence": 0.62,
                    "evidence_event_ids": ["e1", "e2"],
                    "occurred_at": "2026-11-17T08:00:00+00:00",
                    "device_id": "light.hall",
                    "reader_id": "ct_shared",
                },
            ],
        },
    },
}

SAMPLE_DIAGNOSTICS = {
    "errors": {
        "recent_system_events": [
            {
                "category": "configuration_change",
                "timestamp": "2026-11-17T08:00:00+00:00",
                "details": {
                    "event": {
                        "kind": "threshold_tune",
                        "status": "suspected",
                        "confidence": 0.44,
                        "evidence_event_ids": ["e3"],
                    },
                },
            },
            {
                "category": "quality",
                "timestamp": "2026-11-17T07:30:00+00:00",
                "level": "degraded",
                "message": "signal dropped below threshold",
            },
        ],
        "recent_quality_events": [
            {
                "timestamp": "2026-11-17T07:00:00+00:00",
                "code": "noise_spike",
                "severity": "warn",
                "detail": "noise floor rose",
            },
        ],
    },
}

SAMPLE_ISOLATION = {
    "verdicts": [
        {
            "device_id": "light.hall",
            "status": "isolated",
            "occurred_at": "2026-11-17T06:00:00+00:00",
            "reason": "contaminated_window",
            "competing_device_ids": ["light.entry", "ct_shared"],
            "power_window_contaminated": True,
            "window_start": "2026-11-17T05:00:00+00:00",
            "window_end": "2026-11-17T06:00:00+00:00",
        },
    ],
}


def _render_uib() -> dict[str, str]:
    """Render the three B4-UIb cards from the real modules under node."""
    if not node_path():
        pytest.skip("node is not installed; the panel does not need it at runtime")
    program = (
        "import { versionHistoryCard, suspectedChangesCard, eventEvidenceCard } from './view-devices.js';\n"
        f"const SAMPLE_TOPOLOGY = {lit(SAMPLE_TOPOLOGY)};\n"
        f"const SAMPLE_MODEL = {lit(SAMPLE_MODEL)};\n"
        f"const SAMPLE_VERSION = {lit(SAMPLE_VERSION)};\n"
        f"const SAMPLE_STATUS = {lit(SAMPLE_STATUS)};\n"
        f"const SAMPLE_DIAGNOSTICS = {lit(SAMPLE_DIAGNOSTICS)};\n"
        f"const SAMPLE_ISOLATION = {lit(SAMPLE_ISOLATION)};\n"
        "console.log(JSON.stringify({\n"
        "  version: versionHistoryCard(SAMPLE_TOPOLOGY, SAMPLE_MODEL, SAMPLE_VERSION).textContent,\n"
        "  suspected: suspectedChangesCard(SAMPLE_STATUS, SAMPLE_DIAGNOSTICS).textContent,\n"
        "  evidence: eventEvidenceCard(SAMPLE_DIAGNOSTICS, SAMPLE_ISOLATION).textContent,\n"
        "}));\n"
    )
    return run_esm(program, shim=DOM_SHIM)

@pytest.fixture(scope="module")
def rendered_uib() -> dict[str, str]:
    return _render_uib()


def test_panel_renders_version_history(rendered_uib: dict[str, str]) -> None:
    """The version card shows topology version ids and the model's status."""
    text = rendered_uib["version"]
    assert "Version history" in text
    assert "Topology versions" in text and "Model versions" in text
    # Published topology version ids, not just a scalar current version.
    assert "v6" in text and "v7" in text
    # The model's status and confidence come straight from the payload.
    assert "Confirmed" in text
    assert "91%" in text
    assert "Active" in text
    assert "Frozen" in text  # an earlier topology record is not treated as active


def test_panel_renders_suspected_changes(rendered_uib: dict[str, str]) -> None:
    """Suspected changes show their kind, SUSPECTED status and confidence."""
    text = rendered_uib["suspected"]
    assert "Suspected configuration changes" in text
    assert "Reader Reassign" in text and "Threshold Tune" in text
    assert "SUSPECTED" in text
    assert "62%" in text and "44%" in text
    assert "2 evidence events" in text and "1 evidence event" in text
    # A SUSPECTED change carries a not-confirmed marker and is NEVER shown as
    # confirmed — only a literal ``status === 'confirmed'`` would read Confirmed.
    assert "not applied" in text
    assert "Confirmed" not in text


def test_panel_renders_event_evidence(rendered_uib: dict[str, str]) -> None:
    """Event evidence surfaces diagnostics events and isolation verdicts."""
    text = rendered_uib["evidence"]
    assert "Event evidence" in text
    # The diagnostics event log entries, with their published detail.
    assert "configuration_change" in text
    assert "signal dropped below threshold" in text
    assert "noise_spike" in text and "noise floor rose" in text
    assert "Degraded" in text and "Warn" in text
    # The isolation verdict with its window, reason and competing devices.
    assert "Isolation · light.hall" in text
    assert "Isolated" in text
    assert "Window" in text
    assert "contaminated_window" in text
    assert "light.entry, ct_shared" in text


# ---------------------------------------------------------------------------
# TASK-UI-MATRIX-DIAGNOSTIC: the dedicated matrix / internal-process view.
#
# The three new renderers (matrixCellsCard, matrixSourceLedgerCard,
# matrixIdentifiabilityCard) live in the same harness slice as the UIa/UIb cards
# -- all before ``systemCard`` -- so the same DOM shim and slice are reused.
# They read only payloads the panel already fetches: /mapping for the cells and
# /devices + /snapshot for the source ledger and identifiability ranges.  No new
# API field is invented; a genuinely absent field reads "—".
# ---------------------------------------------------------------------------

# A snapshot result mirrors the device results for the ledger/range renderers.
SAMPLE_SNAPSHOT = {
    "timestamp": "2026-11-17T10:00:00+00:00",
    "results": [
        d["latest_result"] for d in SAMPLE_DEVICES if d.get("latest_result")
    ],
}


def _render_matrix() -> dict[str, str]:
    """Render the matrix-view cards from the real modules under node."""
    if not node_path():
        pytest.skip("node is not installed; the panel does not need it at runtime")
    program = (
        "import { matrixPivotCard, matrixCellsCard } from './matrix-cell.js';\n"
        "import { matrixSourceLedgerCard as ledger, matrixIdentifiabilityCard } from './matrix-drill.js';\n"
        f"const SAMPLE_MAPPING = {lit(SAMPLE_MAPPING)};\n"
        f"const SAMPLE_DEVICES = {lit(SAMPLE_DEVICES)};\n"
        f"const SAMPLE_SNAPSHOT = {lit(SAMPLE_SNAPSHOT)};\n"
        "const pivot = matrixPivotCard(SAMPLE_MAPPING);\n"
        "const all = [];\n"
        "const walk = (n) => { if (!(n instanceof Node)) return; all.push(n);"
        " (n.children || []).forEach(walk); };\n"
        "walk(pivot);\n"
        "const rows = all.filter((n) => n.tagName === 'th' && n.dataset && n.dataset.device)"
        ".map((n) => n.textContent);\n"
        "const cols = all.filter((n) => n.tagName === 'th' && n.dataset && n.dataset.reader)"
        ".map((n) => n.textContent);\n"
        "const details = all.filter((n) => n.className &&"
        " n.className.indexOf('matrix-pivot-detail') >= 0).map((n) => n.textContent);\n"
        "const empties = all.filter((n) => n.className &&"
        " n.className.indexOf('matrix-pivot-empty') >= 0).map((n) => n.textContent);\n"
        "console.log(JSON.stringify({\n"
        "  pivot: pivot.textContent,\n"
        "  rows: rows, cols: cols, details: details, empties: empties,\n"
        "  cells: matrixCellsCard(SAMPLE_MAPPING).textContent,\n"
        "  ledger: ledger(SAMPLE_DEVICES, SAMPLE_SNAPSHOT, SAMPLE_MAPPING).textContent,\n"
        "  groups: matrixIdentifiabilityCard(SAMPLE_DEVICES, SAMPLE_SNAPSHOT).textContent,\n"
        "}));\n"
    )
    return run_esm(program, shim=DOM_SHIM)

@pytest.fixture(scope="module")
def rendered_matrix() -> dict:
    return _render_matrix()


def test_matrix_pivot_rows_are_devices(rendered_matrix: dict) -> None:
    """Device rows come from the backend devices axis; CT ids are never rows."""
    rows = rendered_matrix["rows"]
    assert "light.kitchen" in rows
    assert "light.hall" in rows
    assert "Device / reader" not in rows  # the corner header is not a device row
    for ct in SAMPLE_MAPPING["readers"]:
        assert ct not in rows, f"CT reader {ct!r} must not appear as a device row label"


def test_matrix_pivot_columns_are_ct_readers(rendered_matrix: dict) -> None:
    """Column headers come from the backend readers axis; devices are not columns."""
    cols = rendered_matrix["cols"]
    assert "ct_kitchen" in cols
    assert "ct_shared" in cols
    for device in SAMPLE_MAPPING["devices"]:
        assert device not in cols, f"device {device!r} must not appear as a reader column"


def test_matrix_pivot_cell_detail(rendered_matrix: dict) -> None:
    """A mapped cell exposes full detail; unmapped cells show an honest dash."""
    details = rendered_matrix["details"]
    assert details, "mapped cells must carry a detail panel"
    joined = "\n".join(details)
    for value in (
        "Confirmed", "Non-identifiable", "Direct", "Indirect",
        "One-to-one", "Many-to-one", "40.0 W", "50.0 W", "10.0 W", "90.0 W",
        "91%", "95%", "12", "5", "blend_v1", "v3", "v7", "cfg-2026-11-17",
        "ct_measurement", "topology", "Unknown load",
    ):
        assert value in joined, f"missing pivot cell detail value {value!r}"
    # The confirmed cell shows its point estimate; the non-identifiable cell does
    # not get an invented per-device watt (only an honest range in its detail).
    assert "45.0 W" in joined
    # Unmapped device/reader pairs render a dash, never a fabricated value.
    assert rendered_matrix["empties"], "grid should include unmapped pairs as dashes"
    assert all(e == "—" for e in rendered_matrix["empties"])


def test_matrix_view_renders_cells(rendered_matrix: dict) -> None:
    """The matrix cell table shows every published field with its actual value."""
    text = rendered_matrix["cells"]
    assert "Mapping matrix" in text
    # Identity, relation and status/observability come straight from the cell.
    for value in (
        "light.kitchen", "ct_kitchen", "light.hall", "ct_shared",
        "Confirmed", "Non-identifiable", "Direct", "Indirect",
        "One-to-one", "Many-to-one",
        # Bounds and point estimate (non-identifiable cell's estimate is "—").
        "45.0 W", "40.0 W", "50.0 W", "90.0 W",
        # Real cell fields the rich view must expose.
        "91%", "95%", "blend_v1", "12", "5",
        "ct_measurement", "topology", "v3", "v7",
        "cfg-2026-11-17", "Coverage", "Relation: One-to-one",
    ):
        assert value in text, f"missing matrix cell field value {value!r}"
    # The non-identifiable cell has NO per-device watt under Estimate: its only
    # number is the 10.0-90.0 range, so a bare "45.0 W" style estimate must belong
    # to the confirmed cell.  The point estimate is blanked for that cell, and
    # the engine's 0.0 placeholder must never surface as a fabricated watt.
    assert "0.00 W" not in text  # no fabricated point watt for the shared cell


def test_matrix_view_renders_source_ledger(rendered_matrix: dict[str, str]) -> None:
    """Used vs unused sources appear with their published reasons."""
    text = rendered_matrix["ledger"]
    assert "source ledger" in text
    assert "Used this cycle" in text
    assert "Available but unused" in text
    # Used source ids + their code-derived reasons, de-duplicated across devices.
    for value in (
        "CT measurement", "Topology", "rows_in_fit:ct_kitchen",
        "graph_used_for_rows_and_identifiability",
        # Unused sources carry a concrete reason each.
        "Learned models", "no_model_for_device",
        "HA attributes", "no_device_level_attributes",
    ):
        assert value in text, f"missing source ledger value {value!r}"
    # Reasons are shown as published codes, never invented into prose.
    assert "None recorded" not in text


def test_matrix_view_identifiability_no_fabrication(rendered_matrix: dict[str, str]) -> None:
    """A non-identifiable device shows its group range, never a per-device watt."""
    text = rendered_matrix["groups"]
    assert "identifiability" in text
    assert "NON-IDENTIFIABLE" in text
    # The honest group sum floor/cap and the members, from latest_result.
    assert "30.0 W" in text and "90.0 W" in text
    assert "Group total of:" in text
    assert "light.hall" in text and "light.entry" in text
    # Both devices share one member set, so one de-duplicated group range renders.
    # The engine's 0.0 power_w placeholder must NEVER surface as a watt, and no
    # per-device "Estimated power" figure may be presented for this group.
    assert "0.00 W" not in text
    assert "Estimated power" not in text


# ---------------------------------------------------------------------------
# Database reset: a destructive admin control with a mandatory typed
# confirmation.  These tests execute the real reset block (and the real ``api``
# / ``post`` helpers it calls) under node against an event-capable DOM shim,
# rather than pattern-matching source.  The shim lives here in the test file only.
# ---------------------------------------------------------------------------

SAMPLE_RESET = {
    "reset": True,
    "backup_path": "/data/wattmosaic.reset-backup.2026-11-17T10-00-00.db",
    "schema_version": 7,
    "latest_schema_version": 7,
}

# An event-capable DOM stand-in.  It extends the primitives the read-only cards
# need with: id lookup, ``value``/``disabled`` properties, event listeners plus
# a ``fire`` helper, and body/URL/storage globals so the real ``api`` helper can
# run.  ``fetch`` is stubbed by each driver to capture the outgoing request.
def _run_reset_driver(driver_js: str, *, reset_payload: dict | None = None) -> dict:
    """Run the reset-card test against the real modules under node."""
    if not node_path():
        pytest.skip("node is not installed; the panel does not need it at runtime")
    payload = lit(reset_payload if reset_payload is not None else SAMPLE_RESET)
    preamble = (
        "global.__calls = [];\n"
        "global.fetch = async (url, init) => {\n"
        "  global.__calls.push({ url, init });\n"
        "  return { ok: true, status: 200, text: async () => JSON.stringify(" + payload + ") };\n"
        "};\n"
        "const byId = (id) => document.getElementById(id);\n"
    )
    imports = (
        "import { resetDatabaseCard } from './view-diagnostics.js';\n"
    )
    return run_esm(imports + preamble + driver_js, shim=EVENT_DOM_SHIM)

_RESET_DRIVER_GUARD = r"""
(async () => {
  document.body.append(resetDatabaseCard());
  byId('reset-db-button').fire('click');
  const dialog = byId('reset-db-dialog');
  const input = byId('reset-db-confirm-input');
  const confirm = byId('reset-db-confirm');
  const opened = !!dialog;
  const startsDisabled = confirm ? confirm.disabled : null;

  // Clicking confirm WITHOUT typing RESET must not fetch.
  confirm.fire('click');
  await new Promise((r) => setTimeout(r, 0));
  const fetchAfterBareClick = global.__calls.length;

  // A wrong token still leaves confirm disabled and fetches nothing.
  input.value = 'reset';
  input.fire('input');
  const disabledOnWrong = confirm.disabled;
  confirm.fire('click');
  await new Promise((r) => setTimeout(r, 0));
  const fetchAfterWrong = global.__calls.length;

  // The exact token enables confirm.
  input.value = 'RESET';
  input.fire('input');
  const enabledOnExact = !confirm.disabled;

  // Cancel removes the dialog and fetches nothing more.
  byId('reset-db-cancel').fire('click');
  const closed = !byId('reset-db-dialog');

  console.log(JSON.stringify({
    hasButton: !!byId('reset-db-button'),
    opened, startsDisabled, fetchAfterBareClick,
    disabledOnWrong, fetchAfterWrong, enabledOnExact, closed,
    totalFetches: global.__calls.length,
  }));
})();
"""

def test_reset_button_requires_typed_confirmation() -> None:
    """The button opens a dialog whose confirm stays inert until RESET is typed."""
    result = _run_reset_driver(_RESET_DRIVER_GUARD)
    assert result["hasButton"] is True
    assert result["opened"] is True
    assert result["startsDisabled"] is True
    # A single click (bare confirm) causes NO request: the typed guard holds.
    assert result["fetchAfterBareClick"] == 0
    assert result["disabledOnWrong"] is True
    assert result["fetchAfterWrong"] == 0
    assert result["enabledOnExact"] is True
    assert result["closed"] is True
    assert result["totalFetches"] == 0


_RESET_DRIVER_POST = r"""
(async () => {
  document.body.append(resetDatabaseCard());
  byId('reset-db-button').fire('click');
  const input = byId('reset-db-confirm-input');
  input.value = 'RESET';
  input.fire('input');
  byId('reset-db-confirm').fire('click');
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({ calls: global.__calls }));
})();
"""


def test_reset_posts_correct_request() -> None:
    """Confirming with the typed token POSTs the exact guarded body to the route."""
    result = _run_reset_driver(_RESET_DRIVER_POST)
    calls = result["calls"]
    assert len(calls) == 1, f"expected exactly one fetch, got {calls!r}"
    call = calls[0]
    assert call["url"] == "/api/v1/commands/reset-db"
    assert call["init"]["method"] == "POST"
    assert json.loads(call["init"]["body"]) == {"confirm": "RESET"}
    # Auth/envelope conventions: credentials and JSON Accept/Content-Type headers.
    assert call["init"]["credentials"] == "same-origin"
    assert call["init"]["headers"]["Accept"] == "application/json"
    assert call["init"]["headers"]["Content-Type"] == "application/json"


_RESET_DRIVER_SUCCESS = r"""
(async () => {
  document.body.append(resetDatabaseCard());
  byId('reset-db-button').fire('click');
  const input = byId('reset-db-confirm-input');
  input.value = 'RESET';
  input.fire('input');
  byId('reset-db-confirm').fire('click');
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({
    result: byId('reset-db-dialog-result').textContent,
  }));
})();
"""


def test_reset_success_shows_backup_path() -> None:
    """Success renders the returned timestamped backup path and schema version."""
    result = _run_reset_driver(_RESET_DRIVER_SUCCESS)
    text = result["result"]
    assert SAMPLE_RESET["backup_path"] in text
    assert "backup" in text.lower()
    assert "Schema version 7" in text


# ---------------------------------------------------------------------------
# OD-51: dynamic-topology messaging must not present a confirmation gate.
#
# Since the owner decision, the current topology is always authoritative for
# inference and learning; candidate wiring only carries reduced confidence.
# These tests execute the real shipped render functions under node (reusing the
# DOM shim above) against real backend payload shapes -- ``status.learning``
# with an informational ``topology_note`` and ``remedy: None`` -- and assert the
# on-screen text never reverts to a "learning paused / confirm to start" gate.
# ---------------------------------------------------------------------------

# Strings that must NEVER appear in the dynamic-topology messaging.  They are
# the old confirmation-gated wording; if any is restored the tests below fail.
_STALE_GATING_STRINGS = (
    "Learning is paused",
    "confirm the topology to start",
    "being rejected as ambiguous",
    "until the topology is confirmed",
    "penalty until",
)

# Verbatim informational note the backend publishes for a dynamic topology
# (engine/wattmosaic_engine/learning/reporting.py::DYNAMIC_TOPOLOGY_NOTE).
_DYNAMIC_TOPOLOGY_NOTE = (
    "Topology is dynamic (candidate); it is authoritative for inference and "
    "learning and carries a reduced confidence (candidate 0.6 vs confirmed "
    "1.0). No confirmation is required."
)

# A real /status payload shape (main.py): ``learning.topology_note`` and
# ``remedy: None`` for a dynamic topology that is running/learning.
SAMPLE_DYNAMIC_STATUS = {
    "learning": {
        "active": True,
        "effective_state": "learning",
        "topology_note": _DYNAMIC_TOPOLOGY_NOTE,
        "remedy": None,
    },
}

# A candidate (non-confirmed) topology object as /topology serves it.
SAMPLE_CANDIDATE_TOPOLOGY = {
    "version": 7,
    "status": "candidate",
    "channels": [
        {"ct_type": "main", "parent_ct_id": None},
        {"ct_type": "branch", "parent_ct_id": "ct_main"},
    ],
    "devices": [{"device_id": "light.kitchen"}, {"device_id": "light.hall"}],
    "created_at": "2026-11-16T10:00:00+00:00",
}


def _render_topology_messaging() -> dict[str, str | bool]:
    """Render the two dynamic-topology surfaces from the real modules."""
    if not node_path():
        pytest.skip("node is not installed; the panel does not need it at runtime")
    # The base shim needs addEventListener because the banner wires onclick.
    shim = DOM_SHIM + "\nNode.prototype.addEventListener = function () {};\n"
    program = (
        "import { topologySummary } from './view-topology.js';\n"
        "import { topologyLearningBanner } from './view-discover.js';\n"
        f"const STATUS = {lit(SAMPLE_DYNAMIC_STATUS)};\n"
        f"const TOPOLOGY = {lit(SAMPLE_CANDIDATE_TOPOLOGY)};\n"
        "const summary = topologySummary(TOPOLOGY, null);\n"
        "const banner = topologyLearningBanner(STATUS, TOPOLOGY);\n"
        "console.log(JSON.stringify({\n"
        "  summary: summary.textContent,\n"
        "  hasBanner: banner !== null,\n"
        "  banner: banner ? banner.textContent : '',\n"
        "  bannerTone: banner ? banner.dataset.tone : null,\n"
        "}));\n"
    )
    return run_esm(program, shim=shim)

@pytest.fixture(scope="module")
def topology_messaging() -> dict[str, str | bool]:
    return _render_topology_messaging()


def test_topology_messaging_is_dynamic_not_gated(
    topology_messaging: dict[str, str | bool],
) -> None:
    """Topology surfaces carry dynamic/confidence messaging, never a gate."""
    combined = (
        str(topology_messaging["summary"]) + " " + str(topology_messaging["banner"])
    )
    # The old confirmation-gated wording must be gone from BOTH surfaces.
    for stale in _STALE_GATING_STRINGS:
        assert stale.lower() not in combined.lower(), (
            f"stale gating text {stale!r} reappeared in dynamic-topology messaging"
        )
    # A dynamic / authoritative message and the confidence/status framing ARE
    # present: the no-devices empty state no longer demands confirmation, and a
    # candidate topology is described as authoritative with its confidence.
    assert "Dynamic topology" in combined
    assert "authoritative" in combined
    assert "reduced confidence" in combined
    # ADR-0043: pinning is an explicit action, not an optional confirmation gate.
    assert "Review edges" in combined


def test_learning_banner_with_topology_note_is_informational(
    topology_messaging: dict[str, str | bool],
) -> None:
    """A `topology_note` payload renders as info status, not a paused banner."""
    assert topology_messaging["hasBanner"] is True
    # Informational tone, never a warning/bad gate tone.
    assert topology_messaging["bannerTone"] == "info"
    banner = str(topology_messaging["banner"])
    # The backend's real note text is surfaced verbatim, plus the active framing.
    assert _DYNAMIC_TOPOLOGY_NOTE in banner
    assert "learning is active" in banner.lower()
    assert "review edges" in banner.lower()  # navigation action, not a prerequisite
    # And crucially it is NOT a paused/rejected/ambiguous block.
    for stale in _STALE_GATING_STRINGS:
        assert stale.lower() not in banner.lower(), (
            f"stale gating text {stale!r} reappeared in the learning banner"
        )


# The pure discovery-job outcome mapper.  Importing it directly
# only this slice under node exercises the shipped mapping with no DOM, no fetch
# and no timer harness -- it is a pure function.
def test_discovery_status_outcome_maps_states() -> None:
    """The pure helper maps running/complete/failed, surfacing the real error text."""
    if not node_path():
        pytest.skip("node is not installed; the panel does not need it at runtime")
    program = (
        "import { discoveryStatusOutcome } from './view-discover.js';\n"
        "console.log(JSON.stringify({\n"
        "  running: discoveryStatusOutcome({ job_id: 'j', status: 'running' }),\n"
        "  complete: discoveryStatusOutcome({ job_id: 'j', status: 'complete', result: { accepted: [1] } }),\n"
        "  failed: discoveryStatusOutcome({ job_id: 'j', status: 'failed', error: 'CT row measures the whole installation' }),\n"
        "  failedObject: discoveryStatusOutcome({ job_id: 'j', status: 'failed', error: { message: 'engine exploded' } }),\n"
        "}));\n"
    )
    out = run_esm(program, shim=DOM_SHIM)

    assert out["running"] == {"done": False}
    assert out["complete"]["done"] is True
    assert out["complete"]["result"] == {"accepted": [1]}
    assert out["failed"]["done"] is True
    assert out["failed"]["error"] == "CT row measures the whole installation"
    assert out["failedObject"]["error"] == "engine exploded"
