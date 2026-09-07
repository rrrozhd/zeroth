"""Lean Python, explicit HTTP and Node agree on the bounded inventory contract."""

from copy import deepcopy
import json
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from tests.econ_plane.test_source_inventory import inventory, reference_digest, request_payload
from tests.sdk.test_client import _recording_client
from zeroth.econ.plane.cloud.schemas import SdkExecutionEvent
from zeroth.econ.plane.decisioning.schemas import (
    DecisionScheduleCreate,
    VersionComparisonRequest as ServerComparison,
)
from zeroth.econ.source_inventory import SourceWindowInventory as ServerInventory
from zeroth.protocol import (
    DecisionScheduleRequest,
    ExecutionEvent,
    SourceWindowInventory,
    VersionComparisonRequest,
    execution_ids_digest,
)


def test_inventory_schema_and_transport_parity():
    assert SourceWindowInventory.model_json_schema() == ServerInventory.model_json_schema()
    payload = request_payload()
    sdk = VersionComparisonRequest.model_validate(payload)
    server = ServerComparison.model_validate(sdk.model_dump(mode="json"))
    assert server.model_dump(mode="json") == sdk.model_dump(mode="json")
    received = []
    _recording_client(received).compare_versions(sdk)
    assert json.loads(received[0].read()) == server.model_dump(mode="json")
    assert received[0].url.path == "/v1/decisions/compare"


@pytest.mark.parametrize(
    "ids", [[], ["plain"], ['quote"', "line\n", "slash/", "\\", "é", "é", "😀", "\ue000"]]
)
def test_digest_matches_independent_node_and_python_reference(ids):
    node = shutil.which("node")
    assert node is not None, "Node is required for cross-language inventory acceptance"
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            """
import {createHash} from 'node:crypto';
let text = ''; for await (const chunk of process.stdin) text += chunk;
const ids = JSON.parse(text).sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)));
process.stdout.write(createHash('sha256').update(JSON.stringify(ids), 'utf8').digest('hex'));
""",
        ],
        input=json.dumps(ids),
        capture_output=True,
        text=True,
        check=True,
    )
    assert execution_ids_digest(ids) == reference_digest(ids) == result.stdout
    assert execution_ids_digest(list(reversed(ids))) == result.stdout


@pytest.mark.parametrize("ids", [["duplicate", "duplicate"], [""], ["x" * 129], [1], ["\ud800"]])
def test_invalid_id_inventories_are_rejected(ids):
    with pytest.raises(ValueError):
        execution_ids_digest(ids)


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate_run",
        "unknown_terminal",
        "naive_open",
        "backward_window",
        "wrong_empty_digest",
        "too_many_runs",
        "too_many_events",
        "fractional_count",
        "boolean_count",
        "unknown_version",
    ],
)
def test_sdk_and_server_reject_invalid_inventory(fault):
    data = inventory("v1")
    if fault == "duplicate_run":
        data["runs"].append(deepcopy(data["runs"][0]))
    elif fault == "unknown_terminal":
        data["runs"][0]["terminal_state"] = "running"
    elif fault == "naive_open":
        data["opened_at"] = "2026-09-06T00:00:00"
    elif fault == "backward_window":
        data["opened_at"], data["closed_at"] = data["closed_at"], data["opened_at"]
    elif fault == "wrong_empty_digest":
        data["runs"][0]["execution_count"] = 0
    elif fault == "too_many_runs":
        data["runs"] = [dict(data["runs"][0], run_id=str(i)) for i in range(5001)]
    elif fault == "too_many_events":
        data["runs"][0]["execution_count"] = 50000  # second run takes total over the bound
    elif fault == "fractional_count":
        data["runs"][0]["execution_count"] = 1.5
    elif fault == "boolean_count":
        data["runs"][0]["execution_count"] = True
    else:
        data["version"] = "unknown"
    for model in (SourceWindowInventory, ServerInventory):
        with pytest.raises(ValidationError):
            model.model_validate(data)


def test_window_and_inventory_order_normalize_but_assertions_change_identity():
    data = inventory("v1")
    canonical = SourceWindowInventory.model_validate(data).digest()
    data["runs"].reverse()
    data["opened_at"] = "2026-09-05T20:00:00-04:00"
    assert SourceWindowInventory.model_validate(data).digest() == canonical
    data["runs"][0]["terminal_state"] = "failed"
    assert SourceWindowInventory.model_validate(data).digest() != canonical


def test_comparison_requires_both_inventories_and_schedules_reject_fixed_windows():
    payload = request_payload()
    for model in (DecisionScheduleRequest, DecisionScheduleCreate):
        with pytest.raises(ValidationError):
            model.model_validate(payload)
    del payload["source_windows"]["baseline"]
    for model in (VersionComparisonRequest, ServerComparison):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


@pytest.mark.parametrize("model", [ExecutionEvent, SdkExecutionEvent])
def test_windowed_events_require_stable_id_and_timestamp(model):
    data = dict(workflow="invoice", run_id="run", step="extract", source_window_id="batch")
    with pytest.raises(ValidationError, match="explicit event_id"):
        model.model_validate(data)
    data["event_id"] = "event"
    data["recorded_at"] = "2026-09-06T00:00:00"
    with pytest.raises(ValidationError, match="aware recorded_at"):
        model.model_validate(data)
    data["recorded_at"] = "2026-09-05T20:00:00-04:00"
    event = model.model_validate(data)
    assert event.model_dump(mode="json")["recorded_at"] == "2026-09-06T00:00:00Z"
