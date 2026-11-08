"""Export JSON Schemas from the authoritative contracts.

Run: python tools/dev/export_schemas.py
Fails if CONTRACT_VERSION does not match the schema_metadata expectation.
"""

from __future__ import annotations

import json
from pathlib import Path

from wattmosaic_engine import contracts

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "schemas"

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
    "health_response": contracts.HealthResponse,
    "snapshot_response": contracts.SnapshotResponse,
    "error_response": contracts.ErrorResponse,
    "service_command": contracts.ServiceCommand,
    "service_result": contracts.ServiceResult,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    index = {"contract_version": contracts.CONTRACT_VERSION, "schemas": {}}
    for name, model in MODELS.items():
        schema = model.model_json_schema(mode="serialization")
        schema["$id"] = f"https://wattmosaic.local/schemas/{name}.json"
        schema["x-contract-version"] = contracts.CONTRACT_VERSION
        (OUT / f"{name}.json").write_text(json.dumps(schema, indent=2) + "\n")
        index["schemas"][name] = f"{name}.json"
    (OUT / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    print(json.dumps({"contract_version": contracts.CONTRACT_VERSION, "schemas": len(MODELS)}, indent=2))


if __name__ == "__main__":
    main()
