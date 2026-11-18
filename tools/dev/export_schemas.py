"""Export JSON Schemas from the authoritative contracts.

Run: python tools/dev/export_schemas.py
Fails if CONTRACT_VERSION does not match the schema_metadata expectation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Resolve the engine from *this* checkout, not from whatever `pip install -e`
# happens to point at.  A worktree shares the parent's virtualenv, whose editable
# install names the main checkout, so an unqualified import would generate the
# schemas of a different tree and the gate would pass against stale code.
ENGINE_ROOT = ROOT / "engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from wattmosaic_engine import contracts  # noqa: E402
from wattmosaic_engine.contracts import (  # noqa: E402
    entities,
    foundation,
    inference,
    matrix,
    measurement,
    power_models,
    topology,
)

OUT = ROOT / "data" / "schemas"

# The original single-file module's export set.  Kept verbatim so the generated
# schemas are byte-identical to those from before the contracts.py -> contracts/
# split (X01).  New contracts are exported through each submodule's
# ``SCHEMA_EXPORTS`` list instead of being added here.
MODELS = {
    "measurement": contracts.Measurement,
    "ct_channel": contracts.CTChannel,
    "device_topology": contracts.DeviceTopology,
    "topology": contracts.Topology,
    "device_configuration": contracts.DeviceConfiguration,
    "device_configuration_set": contracts.DeviceConfigurationSet,
    "light_configuration": contracts.LightConfiguration,
    "controller_configuration": contracts.ControllerConfiguration,
    "device_event": contracts.DeviceEvent,
    "device_model_version": contracts.DeviceModelVersion,
    "learning_sample": contracts.LearningSample,
    "inference_result": contracts.InferenceResult,
    "reconciliation": contracts.Reconciliation,
    "data_quality_report": contracts.DataQualityReport,
    "topology_edge": contracts.TopologyEdge,
    "topology_version": contracts.TopologyVersion,
    "residual_decomposition_record": contracts.ResidualDecompositionRecord,
    "unknown_load_profile": contracts.UnknownLoadProfile,
    "device_reader_mapping_cell": contracts.DeviceReaderMappingCell,
    "configuration_change_event": contracts.ConfigurationChangeEvent,
    "event_evidence_record": contracts.EventEvidenceRecord,
    "storage_health_report": contracts.StorageHealthReport,
    "unknown_residual_event": contracts.UnknownResidualEvent,
    "residual_baseline_change": contracts.ResidualBaselineChange,
    "health_response": contracts.HealthResponse,
    "snapshot_response": contracts.SnapshotResponse,
    "error_response": contracts.ErrorResponse,
    "service_command": contracts.ServiceCommand,
    "service_result": contracts.ServiceResult,
}

# Submodules that may contribute new schemas.  Each declares ``SCHEMA_EXPORTS``
# as a list of ``(name, model)`` pairs; tasks own their submodule's list, so no
# task needs to edit this exporter.
SCHEMA_MODULES = (
    foundation,
    entities,
    measurement,
    topology,
    inference,
    matrix,
    power_models,
)


def collect_models() -> dict[str, type]:
    """Return the legacy export set plus every submodule's ``SCHEMA_EXPORTS``."""
    collected = dict(MODELS)
    for module in SCHEMA_MODULES:
        for name, model in getattr(module, "SCHEMA_EXPORTS", ()):
            collected[name] = model
    return collected


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    models = collect_models()
    index = {"contract_version": contracts.CONTRACT_VERSION, "schemas": {}}
    for name, model in models.items():
        schema = model.model_json_schema(mode="serialization")
        schema["$id"] = f"https://wattmosaic.local/schemas/{name}.json"
        schema["x-contract-version"] = contracts.CONTRACT_VERSION
        (OUT / f"{name}.json").write_text(json.dumps(schema, indent=2) + "\n")
        index["schemas"][name] = f"{name}.json"
    (OUT / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    print(json.dumps({"contract_version": contracts.CONTRACT_VERSION, "schemas": len(models)}, indent=2))


if __name__ == "__main__":
    main()
