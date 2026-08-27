"""Controlled Workflow 1 bad-credential fixture and evidence validator.

The workflow contains a real OpenAI-backed agent. During this one negative
journey, trusted process configuration points that provider at the local
authentication-rejection sink. The sink observes only that an authorization
credential was supplied and returns HTTP 401; it never stores the credential
or request body and no external provider endpoint is contacted.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .provider_free_composed import Request, _object, _post, _publish_deploy_workflow


@dataclass(frozen=True, slots=True)
class BadCredentialFixture:
    schema_version: int
    fixture_id: str
    workflow_id: str
    graph_version_ref: str
    deployment_ref: str
    deployment_version: int
    payload: dict[str, str]
    provider_kind: str = "controlled_local_auth_rejection"
    external_provider_calls: int = 0
    restart_required: bool = True


def provision_bad_credential_fixture(*, request: Request, fixture_id: str) -> BadCredentialFixture:
    """Publish and deploy a real zero-retry agent graph for the 401 journey."""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,47}", fixture_id) is None:
        raise ValueError("fixture_id must be a short lowercase slug")
    contract_prefix = f"contract://workflow1-bad-credential-{fixture_id}"
    input_contract = f"{contract_prefix}.input"
    output_contract = f"{contract_prefix}.output"
    for name, schema in (
        (
            input_contract,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["question"],
                "properties": {"question": {"type": "string", "minLength": 1}},
            },
        ),
        (
            output_contract,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["answer"],
                "properties": {"answer": {"type": "string", "minLength": 1}},
            },
        ),
    ):
        contract = _post(
            request,
            "/api/studio/v1/contracts",
            {
                "name": name,
                "json_schema": schema,
                "metadata": {"campaign_slice": "workflow1-bad-credential"},
            },
            expected=201,
            label=f"create bad-credential contract {name}",
        )
        if contract.get("name") != name or contract.get("version") != 1:
            raise RuntimeError(f"contract {name} is not a new immutable v1 fixture")

    created = _post(
        request,
        "/api/studio/v1/workflows",
        {"name": f"Workflow 1 bad credential {fixture_id}"},
        expected=201,
        label="create bad-credential workflow",
    )
    workflow_id = created.get("id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise RuntimeError("bad-credential workflow identity is missing")
    retry_policy = {
        "max_retries": 0,
        "retry_on_validation_error": False,
        "retry_on_provider_error": False,
        "retry_on_timeout": False,
        "backoff_seconds": 0.0,
        "base_delay": 0.0,
        "max_delay": 0.0,
        "use_exponential_backoff": False,
    }
    payload = {
        "entry_step": "request",
        "nodes": [
            {
                "id": "request",
                "type": "entrypoint",
                "position": {"x": 0, "y": 0},
                "data": {
                    "label": "Research question",
                    "config": {},
                    "input_contract_ref": input_contract,
                    "output_contract_ref": input_contract,
                },
            },
            {
                "id": "answer",
                "type": "agent",
                "position": {"x": 360, "y": 0},
                "data": {
                    "label": "Credential-checked answer",
                    "config": {
                        "instruction": "Return a JSON object with one non-empty answer field.",
                        "model_provider": "openai/gpt-4o-mini",
                        "retry_policy": retry_policy,
                        "timeout_seconds": 20,
                    },
                    "input_contract_ref": input_contract,
                    "output_contract_ref": output_contract,
                },
            },
        ],
        "edges": [
            {
                "id": "request-answer",
                "source": "request",
                "target": "answer",
                "kind": "data",
            }
        ],
        "execution_settings": {
            "max_total_steps": 3,
            "max_total_runtime_seconds": 30,
            "max_visits_per_node": 1,
            "max_visits_per_edge": 1,
            "default_timeout_seconds": 20,
        },
    }
    _object(
        request("PUT", f"/api/studio/v1/workflows/{workflow_id}", payload),
        expected=200,
        label="save bad-credential workflow",
    )
    deployment_ref = f"workflow1-bad-credential-{fixture_id}"
    graph_version_ref, deployment_version = _publish_deploy_workflow(
        request=request,
        workflow_id=workflow_id,
        deployment_ref=deployment_ref,
    )
    return BadCredentialFixture(
        schema_version=1,
        fixture_id=fixture_id,
        workflow_id=workflow_id,
        graph_version_ref=graph_version_ref,
        deployment_ref=deployment_ref,
        deployment_version=deployment_version,
        payload={"question": "Explain why authentication failures must fail closed."},
    )


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be an object")
    return value


def validate_bad_credential_summary(
    summary: Mapping[str, Any], *, expected_deployment_ref: str, expected_graph_version_ref: str
) -> dict[str, object]:
    """Require a resolved credential, local 401, signed run, and exact restore."""
    if summary.get("schema_version") != 1:
        raise RuntimeError("bad-credential summary schema is unsupported")
    if (
        summary.get("deployment_ref") != expected_deployment_ref
        or summary.get("graph_version_ref") != expected_graph_version_ref
    ):
        raise RuntimeError("bad-credential fixture identity is not exact")
    if summary.get("external_provider_calls") != 0:
        raise RuntimeError("bad-credential journey contacted an external provider")
    sink = _mapping(summary.get("authentication_sink"), label="authentication sink")
    request_count = sink.get("request_count")
    if not isinstance(request_count, int) or request_count < 1 or request_count > 3:
        raise RuntimeError("authentication sink request count is outside the bounded range")
    if (
        sink.get("loopback_only") is not True
        or sink.get("authorization_present") is not True
        or sink.get("authorization_value_retained") is not False
        or sink.get("request_body_retained") is not False
        or sink.get("response_status") != 401
    ):
        raise RuntimeError("authentication sink did not prove a sanitized credential rejection")
    run = _mapping(summary.get("run"), label="bad-credential run")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise RuntimeError("bad-credential run identity is invalid")
    if (
        run.get("status") != "failed"
        or run.get("failure_reason") != "node_execution_failed"
        or run.get("timeline_node_ids") != ["request", "answer"]
        or run.get("timeline_statuses") != ["completed", "failed"]
    ):
        raise RuntimeError("bad-credential terminal runtime state is invalid")
    if (
        run.get("audit_verified") is not True
        or run.get("signature_verified") is not True
        or run.get("audit_record_count") != 3
        or run.get("unsigned_record_count") != 0
    ):
        raise RuntimeError("bad-credential signed audit proof is invalid")
    if run.get("provider_request_ids") != []:
        raise RuntimeError("rejected credential produced a provider request identity")
    cost_event_ids = run.get("cost_event_ids")
    if (
        not isinstance(cost_event_ids, list)
        or len(cost_event_ids) != 1
        or not isinstance(cost_event_ids[0], str)
        or not cost_event_ids[0]
    ):
        raise RuntimeError("rejected credential did not retain one cost identity")
    if (
        run.get("priced_call_count") != 1
        or run.get("cost_event_count") != 1
        or run.get("total_cost_usd") != 0.0
        or run.get("cost_identity_state") != "correlated"
        or run.get("reconciliation_state") != "reconciled"
    ):
        raise RuntimeError("bad-credential zero-cost economics are invalid")
    reservation = _mapping(run.get("reservation"), label="bad-credential reservation")
    if (
        reservation.get("status") != "committed"
        or reservation.get("held_cost_usd") != 0.0
        or reservation.get("actual_cost_usd") != 0.0
        or reservation.get("released_cost_usd") != 0.00262185
        or reservation.get("cost_measurement") != "measured"
        or reservation.get("cleanup_status") != "controlled_authentication_rejection"
    ):
        raise RuntimeError("bad-credential reservation was not reconciled exactly")
    refresh = _mapping(run.get("refresh"), label="bad-credential refresh")
    if (
        refresh.get("before_run_id") != run_id
        or refresh.get("restored_run_id") != run_id
        or refresh.get("restored_status") != "failed"
    ):
        raise RuntimeError("bad-credential refresh persistence is invalid")
    restore = _mapping(summary.get("d012_restore"), label="D-012 restore")
    if restore.get("exact") is not True or restore.get("before") != restore.get("after"):
        raise RuntimeError("D-012 was not restored exactly")
    return {
        "run_id": run_id,
        "local_authentication_requests": request_count,
        "external_provider_calls": 0,
        "total_cost_usd": 0.0,
        "d012_restored": True,
    }
