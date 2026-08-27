from __future__ import annotations

from copy import deepcopy

import pytest

from release.live_evaluation.workflow1_provider_faults_live import (
    EXPECTED_MODES,
    provision_provider_fault_fixture,
    validate_provider_fault_summary,
)


DEPLOYMENT = "provider-free-w1-provider-faults-w1-faults-20260826a"
GRAPH = "workflow-1-provider-faults@1"


class _Response:
    def __init__(self, status_code: int, value: object) -> None:
        self.status_code = status_code
        self.value = value
        self.text = "safe"

    def json(self) -> object:
        return self.value


def test_fixture_publishes_one_agent_with_explicit_zero_retries() -> None:
    puts: list[dict[str, object]] = []

    def request(method: str, path: str, payload: dict[str, object] | None) -> _Response:
        if method == "POST" and path == "/api/studio/v1/contracts":
            assert payload is not None
            return _Response(201, {"name": payload["name"], "version": 1})
        if method == "POST" and path == "/api/studio/v1/workflows":
            return _Response(201, {"id": "workflow-1-provider-faults"})
        if method == "PUT" and path == "/api/studio/v1/workflows/workflow-1-provider-faults":
            assert payload is not None
            puts.append(payload)
            return _Response(200, {"id": "workflow-1-provider-faults"})
        if method == "POST" and path.endswith("/preflight"):
            return _Response(200, {"ready": True, "issues": []})
        if method == "POST" and path.endswith("/publish"):
            return _Response(200, {"status": "published", "version": 1})
        if method == "POST" and path == "/v1/deployments":
            return _Response(
                201,
                {
                    "deployment_ref": DEPLOYMENT,
                    "version": 1,
                    "graph_version_ref": GRAPH,
                },
            )
        raise AssertionError((method, path))

    fixture = provision_provider_fault_fixture(
        request=request, fixture_id="w1-faults-20260826a"
    )

    assert fixture.deployment_ref == DEPLOYMENT
    assert fixture.graph_version_ref == GRAPH
    assert fixture.provider_calls_performed == 0
    assert fixture.fault_modes == EXPECTED_MODES
    nodes = puts[0]["nodes"]
    assert isinstance(nodes, list)
    assert [node["id"] for node in nodes] == ["request", "answer"]
    config = nodes[1]["data"]["config"]
    assert config["model_provider"] == "openai/gpt-4o-mini"
    assert config["retry_policy"] == {
        "max_retries": 0,
        "retry_on_validation_error": False,
        "retry_on_provider_error": False,
        "retry_on_timeout": False,
        "backoff_seconds": 0.0,
        "base_delay": 0.0,
        "max_delay": 0.0,
        "use_exponential_backoff": False,
    }


def _summary() -> dict[str, object]:
    cases = []
    for index, mode in enumerate(EXPECTED_MODES, start=1):
        run_id = f"{index:x}" * 32
        fault_id = f"{index + 3:x}" * 32
        cases.append(
            {
                "mode": mode,
                "fault_id": fault_id,
                "fault_consumed": True,
                "run_id": run_id,
                "status": "failed",
                "failure_reason": "node_execution_failed",
                "timeline_node_ids": ["request", "answer"],
                "timeline_statuses": ["completed", "failed"],
                "audit_verified": True,
                "signature_verified": True,
                "audit_record_count": 2,
                "unsigned_record_count": 0,
                "provider_request_ids": [],
                "cost_event_ids": [],
                "priced_call_count": 0,
                "cost_event_count": 0,
                "total_cost_usd": 0.0,
                "cost_identity_state": "not_applicable_no_priced_call",
                "reconciliation_state": "reconciled_zero_activity",
                "refresh": {
                    "before_run_id": run_id,
                    "restored_run_id": run_id,
                    "restored_status": "failed",
                },
            }
        )
    return {
        "schema_version": 1,
        "deployment_ref": DEPLOYMENT,
        "graph_version_ref": GRAPH,
        "provider_calls_performed": 0,
        "cases": cases,
        "d012_restore": {"exact": True, "before": {"deployment_ref": "d012"}, "after": {"deployment_ref": "d012"}},
    }


def test_validator_accepts_exact_three_case_provider_free_matrix() -> None:
    validated = validate_provider_fault_summary(
        _summary(),
        expected_deployment_ref=DEPLOYMENT,
        expected_graph_version_ref=GRAPH,
    )

    assert validated == {
        "modes": list(EXPECTED_MODES),
        "run_ids": ["1" * 32, "2" * 32, "3" * 32],
        "provider_calls_performed": 0,
        "total_cost_usd": 0.0,
        "d012_restored": True,
    }


@pytest.mark.parametrize(
    ("case_index", "field", "value", "message"),
    [
        (0, "fault_consumed", False, "consumed"),
        (1, "signature_verified", False, "signed audit"),
        (2, "provider_request_ids", ["provider-1"], "provider identity"),
        (0, "cost_event_ids", ["cost-1"], "cost identity"),
        (1, "restored_status", "running", "refresh"),
    ],
)
def test_validator_rejects_relabelled_or_nonzero_activity(
    case_index: int, field: str, value: object, message: str
) -> None:
    summary = deepcopy(_summary())
    cases = summary["cases"]
    assert isinstance(cases, list)
    case = cases[case_index]
    if field == "restored_status":
        case["refresh"][field] = value
    else:
        case[field] = value

    with pytest.raises(RuntimeError, match=message):
        validate_provider_fault_summary(
            summary,
            expected_deployment_ref=DEPLOYMENT,
            expected_graph_version_ref=GRAPH,
        )
