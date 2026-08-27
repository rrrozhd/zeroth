from __future__ import annotations

from copy import deepcopy

import pytest

from release.live_evaluation.workflow1_bad_credential_live import (
    provision_bad_credential_fixture,
    validate_bad_credential_summary,
)


DEPLOYMENT = "workflow1-bad-credential-bad-credential-20260826a"
GRAPH = "workflow-1-bad-credential@1"


class _Response:
    def __init__(self, status_code: int, value: object) -> None:
        self.status_code = status_code
        self.value = value
        self.text = "safe"

    def json(self) -> object:
        return self.value


def test_fixture_publishes_real_openai_agent_with_zero_runtime_retries() -> None:
    puts: list[dict[str, object]] = []

    def request(method: str, path: str, payload: dict[str, object] | None) -> _Response:
        if method == "POST" and path == "/api/studio/v1/contracts":
            assert payload is not None
            return _Response(201, {"name": payload["name"], "version": 1})
        if method == "POST" and path == "/api/studio/v1/workflows":
            return _Response(201, {"id": "workflow-1-bad-credential"})
        if method == "PUT" and path == "/api/studio/v1/workflows/workflow-1-bad-credential":
            assert payload is not None
            puts.append(payload)
            return _Response(200, {"id": "workflow-1-bad-credential"})
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

    fixture = provision_bad_credential_fixture(
        request=request, fixture_id="bad-credential-20260826a"
    )

    assert fixture.deployment_ref == DEPLOYMENT
    assert fixture.graph_version_ref == GRAPH
    assert fixture.external_provider_calls == 0
    nodes = puts[0]["nodes"]
    assert isinstance(nodes, list)
    assert [node["id"] for node in nodes] == ["request", "answer"]
    agent = nodes[1]["data"]["config"]
    assert agent["model_provider"] == "openai/gpt-4o-mini"
    assert agent["retry_policy"]["max_retries"] == 0
    assert agent["retry_policy"]["retry_on_provider_error"] is False


def _summary() -> dict[str, object]:
    run_id = "a" * 32
    return {
        "schema_version": 1,
        "deployment_ref": DEPLOYMENT,
        "graph_version_ref": GRAPH,
        "external_provider_calls": 0,
        "authentication_sink": {
            "request_count": 1,
            "loopback_only": True,
            "authorization_present": True,
            "authorization_value_retained": False,
            "request_body_retained": False,
            "response_status": 401,
        },
        "run": {
            "run_id": run_id,
            "status": "failed",
            "failure_reason": "node_execution_failed",
            "timeline_node_ids": ["request", "answer"],
            "timeline_statuses": ["completed", "failed"],
            "audit_verified": True,
            "signature_verified": True,
            "audit_record_count": 3,
            "unsigned_record_count": 0,
            "provider_request_ids": [],
            "cost_event_ids": ["probe-controlled-auth-rejection"],
            "priced_call_count": 1,
            "cost_event_count": 1,
            "total_cost_usd": 0.0,
            "cost_identity_state": "correlated",
            "reconciliation_state": "reconciled",
            "reservation": {
                "status": "committed",
                "held_cost_usd": 0.0,
                "actual_cost_usd": 0.0,
                "released_cost_usd": 0.00262185,
                "cost_measurement": "measured",
                "cleanup_status": "controlled_authentication_rejection",
            },
            "refresh": {
                "before_run_id": run_id,
                "restored_run_id": run_id,
                "restored_status": "failed",
            },
        },
        "d012_restore": {
            "exact": True,
            "before": {"deployment_ref": "d012"},
            "after": {"deployment_ref": "d012"},
        },
    }


def test_validator_accepts_exact_sanitized_local_401_proof() -> None:
    assert validate_bad_credential_summary(
        _summary(),
        expected_deployment_ref=DEPLOYMENT,
        expected_graph_version_ref=GRAPH,
    ) == {
        "run_id": "a" * 32,
        "local_authentication_requests": 1,
        "external_provider_calls": 0,
        "total_cost_usd": 0.0,
        "d012_restored": True,
    }


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("authentication_sink", "authorization_present"), False, "sanitized"),
        (("authentication_sink", "authorization_value_retained"), True, "sanitized"),
        (("authentication_sink", "request_count"), 4, "bounded"),
        (("run", "signature_verified"), False, "signed audit"),
        (("run", "provider_request_ids"), ["provider-1"], "provider request"),
        (("run", "cost_event_ids"), [], "cost identity"),
        (("run", "reservation"), {"status": "ambiguous"}, "reservation"),
        (("run", "restored_status"), "running", "refresh"),
    ],
)
def test_validator_rejects_leaky_unbounded_or_unverified_proof(
    path: tuple[str, str], value: object, message: str
) -> None:
    summary = deepcopy(_summary())
    if path[1] == "restored_status":
        summary["run"]["refresh"][path[1]] = value
    else:
        summary[path[0]][path[1]] = value
    with pytest.raises(RuntimeError, match=message):
        validate_bad_credential_summary(
            summary,
            expected_deployment_ref=DEPLOYMENT,
            expected_graph_version_ref=GRAPH,
        )
