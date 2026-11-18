"""Data-rendering tests for the panel's derived backend-data cards (B4-UIa).

``addon/ui/app.js`` is a plain browser script with no module seam, so — exactly
as ``test_panel.py`` does for the window helpers — these tests *execute* a slice
of the real shipped script under node rather than pattern-matching it.  A tiny
DOM shim implements the handful of primitives the render helpers use
(``document.createElement``, ``Node``, ``textContent``, ``append``), so the
assertions are on the text the browser would actually put on screen when the
engine publishes residual decomposition, the mapping matrix and identifiability
group ranges.

This is deliberately different from the shell/asset guards in ``test_panel.py``:
those pin that files are *served*; these assert that given a sample API payload,
the rendered DOM contains the expected fields and values.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_JS = REPO_ROOT / "addon" / "ui" / "app.js"

# The slice of the IIFE that holds the DOM helpers, formatters, STRINGS table and
# the three derived-data renderers (up to the first unrelated card).  It contains
# only declarations, so evaluating it under node executes no boot/fetch/DOM work.
_SLICE_START = "const el = (tag, props = {}, children = []) => {"
_SLICE_END = "function systemCard(health, status) {"

# Minimal DOM stand-in.  ``el`` uses ``child instanceof Node`` and appends text
# nodes; ``textContent`` concatenates the tree the way a browser would, which is
# what the assertions below read.
_DOM_SHIM = r"""
class Node {
  constructor() {
    this.children = [];
    this._text = '';
    this.dataset = {};
    this.attrs = {};
    this.className = '';
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    return this._text + this.children.map(
      (c) => (c instanceof Node ? c.textContent : String(c))
    ).join('');
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  append(...cs) { for (const c of cs) this.children.push(c); }
  get firstChild() { return this.children[0] || null; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); }
}
global.Node = Node;
global.document = {
  createElement: (tag) => { const n = new Node(); n.tagName = tag; return n; },
  createTextNode: (t) => { const n = new Node(); n._text = String(t); return n; },
  // ``frag`` (used by ``kv``, which the mapping card renders) builds a fragment
  // and appends children to it; a Node has both ``append`` and textContent.
  createDocumentFragment: () => new Node(),
  getElementById: () => null,
  querySelectorAll: () => [],
};
"""


def _render(payloads: dict) -> dict[str, str]:
    """Render the three data cards under node and return their text content.

    Returns ``None``-carrying callers as empty text where a card legitimately
    renders nothing (no group ranges).  Skips when node is not installed: the
    panel itself needs no node at runtime.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the panel does not need it at runtime")

    script = APP_JS.read_text(encoding="utf-8")
    start = script.index(_SLICE_START)
    end = script.index(_SLICE_END, start)
    block = script[start:end]

    literals = (
        f"const SAMPLE_RESIDUALS = {json.dumps(payloads['residuals'])};\n"
        f"const SAMPLE_MAPPING = {json.dumps(payloads['mapping'])};\n"
        f"const SAMPLE_DEVICES = {json.dumps(payloads['devices'])};\n"
    )
    program = (
        _DOM_SHIM
        + "\n"
        + block
        + "\n"
        + literals
        + "\n"
        + "const groups = groupRangesCard(SAMPLE_DEVICES);\n"
        + "console.log(JSON.stringify({\n"
        + "  residual: residualDecompositionCard(SAMPLE_RESIDUALS).textContent,\n"
        + "  mapping: mappingMatrixCard(SAMPLE_MAPPING).textContent,\n"
        + "  groups: groups ? groups.textContent : '',\n"
        + "}));\n"
    )
    completed = subprocess.run(
        [node, "-e", program],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


# Sample API payloads — shapes taken from the actual engine wire forms:
#   GET /api/v1/residuals -> {window_hours, count, components:[...]}
#   GET /api/v1/mapping   -> {cells:[...], coverage:{counts:{...}}}
#   GET /api/v1/devices   -> [{device_id, name, latest_result:{min_w,max_w,
#                              group_members,power_w}, ...}]
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
    """Render the three B4-UIb cards under node and return their text content."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the panel does not need it at runtime")

    script = APP_JS.read_text(encoding="utf-8")
    start = script.index(_SLICE_START)
    end = script.index(_SLICE_END, start)
    block = script[start:end]

    literals = "\n".join(
        [
            f"const SAMPLE_TOPOLOGY = {json.dumps(SAMPLE_TOPOLOGY)};",
            f"const SAMPLE_MODEL = {json.dumps(SAMPLE_MODEL)};",
            f"const SAMPLE_VERSION = {json.dumps(SAMPLE_VERSION)};",
            f"const SAMPLE_STATUS = {json.dumps(SAMPLE_STATUS)};",
            f"const SAMPLE_DIAGNOSTICS = {json.dumps(SAMPLE_DIAGNOSTICS)};",
            f"const SAMPLE_ISOLATION = {json.dumps(SAMPLE_ISOLATION)};",
        ]
    )
    program = (
        _DOM_SHIM
        + "\n"
        + block
        + "\n"
        + literals
        + "\n"
        + "console.log(JSON.stringify({\n"
        + "  version: versionHistoryCard(SAMPLE_TOPOLOGY, SAMPLE_MODEL, SAMPLE_VERSION).textContent,\n"
        + "  suspected: suspectedChangesCard(SAMPLE_STATUS, SAMPLE_DIAGNOSTICS).textContent,\n"
        + "  evidence: eventEvidenceCard(SAMPLE_DIAGNOSTICS, SAMPLE_ISOLATION).textContent,\n"
        + "}));\n"
    )
    completed = subprocess.run(
        [node, "-e", program],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


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
    """Render the three matrix-view cards under node and return their text."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the panel does not need it at runtime")

    script = APP_JS.read_text(encoding="utf-8")
    start = script.index(_SLICE_START)
    end = script.index(_SLICE_END, start)
    block = script[start:end]

    literals = "\n".join(
        [
            f"const SAMPLE_MAPPING = {json.dumps(SAMPLE_MAPPING)};",
            f"const SAMPLE_DEVICES = {json.dumps(SAMPLE_DEVICES)};",
            f"const SAMPLE_SNAPSHOT = {json.dumps(SAMPLE_SNAPSHOT)};",
        ]
    )
    program = (
        _DOM_SHIM
        + "\n"
        + block
        + "\n"
        + literals
        + "\n"
        + "console.log(JSON.stringify({\n"
        + "  cells: matrixCellsCard(SAMPLE_MAPPING).textContent,\n"
        + "  ledger: matrixSourceLedgerCard(SAMPLE_DEVICES, SAMPLE_SNAPSHOT, SAMPLE_MAPPING).textContent,\n"
        + "  groups: matrixIdentifiabilityCard(SAMPLE_DEVICES, SAMPLE_SNAPSHOT).textContent,\n"
        + "}));\n"
    )
    completed = subprocess.run(
        [node, "-e", program],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def rendered_matrix() -> dict[str, str]:
    return _render_matrix()


def test_matrix_view_renders_cells(rendered_matrix: dict[str, str]) -> None:
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

_RESET_BLOCK_START = "/* RESET_UI_BLOCK_START */"
_RESET_BLOCK_END = "/* RESET_UI_BLOCK_END */"
# The real api/get/post helpers and ApiError live before the el slice; include
# them so the driver exercises the shipped request path, not a reimplementation.
_API_SLICE_START = "  const BASE = (() => {"

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
_RESET_DOM_SHIM = r"""
class Node {
  constructor() {
    this.children = [];
    this._text = '';
    this.dataset = {};
    this.attrs = {};
    this.className = '';
    this._listeners = {};
    this._parent = null;
    this.value = '';
    this.disabled = false;
    this.id = '';
    this.tagName = '';
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    return this._text + this.children.map(
      (c) => (c instanceof Node ? c.textContent : String(c))
    ).join('');
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
  fire(type, ev) {
    for (const fn of (this._listeners[type] || [])) fn.call(this, ev || { target: this });
  }
  append(...cs) {
    for (const c of cs) {
      if (c instanceof Node) c._parent = this;
      this.children.push(c);
    }
  }
  remove() {
    if (this._parent) {
      const i = this._parent.children.indexOf(this);
      if (i >= 0) this._parent.children.splice(i, 1);
      this._parent = null;
    }
  }
  get firstChild() { return this.children[0] || null; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); }
}
global.Node = Node;
function _walk(node, out) {
  for (const c of node.children) {
    if (c instanceof Node) { if (c.id) out.push(c); _walk(c, out); }
  }
  return out;
}
const _body = new Node(); _body.tagName = 'body';
global.document = {
  baseURI: 'http://localhost/',
  body: _body,
  createElement: (tag) => { const n = new Node(); n.tagName = tag; return n; },
  createTextNode: (t) => { const n = new Node(); n._text = String(t); return n; },
  createDocumentFragment: () => new Node(),
  getElementById: (id) => _walk(_body, []).find((n) => n.id === id) || null,
  querySelectorAll: () => [],
};
global.location = { href: 'http://localhost/', hash: '', replace() {} };
global.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
"""


def _run_reset_driver(driver_js: str, *, reset_payload: dict | None = None) -> dict:
    """Evaluate the real api/post helpers + reset block, then a node driver.

    ``driver_js`` may use ``global.__calls`` (captured fetch calls), ``byId`` and
    the ``SAMPLE_RESET`` payload.  It must finish with a ``console.log(JSON...)``.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the panel does not need it at runtime")

    script = APP_JS.read_text(encoding="utf-8")
    api_start = script.index(_API_SLICE_START)
    core_start = script.index(_SLICE_START)
    core_end = script.index(_SLICE_END, core_start)
    api = script[api_start:core_start]
    core = script[core_start:core_end]
    reset_start = script.index(_RESET_BLOCK_START)
    reset_end = script.index(_RESET_BLOCK_END, reset_start)
    reset_block = script[reset_start:reset_end]

    payload = json.dumps(reset_payload if reset_payload is not None else SAMPLE_RESET)
    preamble = (
        "global.__calls = [];\n"
        + "global.fetch = async (url, init) => {\n"
        + "  global.__calls.push({ url, init });\n"
        + "  return { ok: true, status: 200, text: async () => JSON.stringify(" + payload + ") };\n"
        + "};\n"
        + "const byId = (id) => document.getElementById(id);\n"
    )
    program = (
        _RESET_DOM_SHIM + "\n" + preamble + "\n"
        + api + "\n" + core + "\n" + reset_block + "\n" + driver_js + "\n"
    )
    completed = subprocess.run(
        [node, "-e", program],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


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
