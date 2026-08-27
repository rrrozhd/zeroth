"""Provider-free Workflow 1 deterministic provider-fault fixture.

The published graph contains a real agent node, but every accepted case arms a
campaign-scoped one-shot fault before the node runs.  Its explicit zero-retry
policy makes the local fault terminal, so the wrapped provider is never called.
This module provisions the immutable fixture and validates a sanitized browser
and runtime summary; it does not itself claim live-provider execution.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .provider_free_composed import Request, _object, _post, _publish_deploy_workflow

EXPECTED_MODES = ("timeout", "rate_limit", "malformed_response")


@dataclass(frozen=True, slots=True)
class ProviderFreeFaultFixture:
    schema_version: int
    fixture_id: str
    workflow_id: str
    graph_version_ref: str
    deployment_ref: str
    deployment_version: int
    payload: dict[str, str]
    fault_modes: tuple[str, ...] = EXPECTED_MODES
    provider_calls_performed: int = 0
    provider_economics_status: str = "not_applicable_no_priced_call"
    restart_required: bool = True


def provision_provider_fault_fixture(
    *, request: Request, fixture_id: str
) -> ProviderFreeFaultFixture:
    """Publish one real agent graph with a terminal, explicit zero-retry policy."""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,47}", fixture_id) is None:
        raise ValueError("fixture_id must be a short lowercase slug")
    contract_prefix = f"contract://provider-free-w1-provider-faults-{fixture_id}"
    input_contract = f"{contract_prefix}.input"
    output_contract = f"{contract_prefix}.output"
    contracts = (
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
    )
    for name, schema in contracts:
        contract = _post(
            request,
            "/api/studio/v1/contracts",
            {
                "name": name,
                "json_schema": schema,
                "metadata": {
                    "campaign_slice": "workflow1-deterministic-provider-faults",
                    "provider_calls_performed": 0,
                },
            },
            expected=201,
            label=f"create provider-fault contract {name}",
        )
        if contract.get("name") != name or contract.get("version") != 1:
            raise RuntimeError(f"contract {name} is not a new immutable v1 fixture")

    created = _post(
        request,
        "/api/studio/v1/workflows",
        {"name": f"Workflow 1 deterministic provider faults {fixture_id}"},
        expected=201,
        label="create provider-fault workflow",
    )
    workflow_id = created.get("id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise RuntimeError("provider-fault workflow identity is missing")

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
    nodes = [
        {
            "id": "request",
            "type": "entrypoint",
            "position": {"x": 0, "y": 0},
            "data": {
                "label": "Deterministic research question",
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
                "label": "Fault-controlled answer",
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
    ]
    payload = {
        "entry_step": "request",
        "nodes": nodes,
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
        label="save provider-fault workflow",
    )
    deployment_ref = f"provider-free-w1-provider-faults-{fixture_id}"
    graph_version_ref, deployment_version = _publish_deploy_workflow(
        request=request,
        workflow_id=workflow_id,
        deployment_ref=deployment_ref,
    )
    return ProviderFreeFaultFixture(
        schema_version=1,
        fixture_id=fixture_id,
        workflow_id=workflow_id,
        graph_version_ref=graph_version_ref,
        deployment_ref=deployment_ref,
        deployment_version=deployment_version,
        payload={"question": "Explain why deterministic evaluation faults are local."},
    )


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{label} must be an object")
    return value


def _sequence(value: object, *, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise RuntimeError(f"{label} must be an array")
    return value


def validate_provider_fault_summary(
    summary: Mapping[str, Any], *, expected_deployment_ref: str, expected_graph_version_ref: str
) -> dict[str, object]:
    """Require exact local faults, signed audits, refresh, and zero priced activity."""
    if summary.get("schema_version") != 1:
        raise RuntimeError("provider-fault summary schema is unsupported")
    if (
        summary.get("deployment_ref") != expected_deployment_ref
        or summary.get("graph_version_ref") != expected_graph_version_ref
    ):
        raise RuntimeError("provider-fault fixture identity is not exact")
    if summary.get("provider_calls_performed") != 0:
        raise RuntimeError("provider-fault evidence reports provider activity")
    cases = _sequence(summary.get("cases"), label="provider-fault cases")
    if len(cases) != len(EXPECTED_MODES):
        raise RuntimeError("provider-fault matrix must contain exactly three cases")

    run_ids: list[str] = []
    total_cost = 0.0
    for expected_mode, raw_case in zip(EXPECTED_MODES, cases, strict=True):
        case = _mapping(raw_case, label=f"{expected_mode} case")
        if case.get("mode") != expected_mode:
            raise RuntimeError("provider-fault cases are not in the exact expected order")
        fault_id = case.get("fault_id")
        if not isinstance(fault_id, str) or re.fullmatch(r"[0-9a-f]{32}", fault_id) is None:
            raise RuntimeError(f"{expected_mode} fault identity is invalid")
        if case.get("fault_consumed") is not True:
            raise RuntimeError(f"{expected_mode} deterministic fault was not consumed")
        run_id = case.get("run_id")
        if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
            raise RuntimeError(f"{expected_mode} run identity is invalid")
        run_ids.append(run_id)
        if (
            case.get("status") != "failed"
            or case.get("failure_reason") != "node_execution_failed"
            or case.get("timeline_node_ids") != ["request", "answer"]
            or case.get("timeline_statuses") != ["completed", "failed"]
        ):
            raise RuntimeError(f"{expected_mode} terminal runtime state is invalid")
        if (
            case.get("audit_verified") is not True
            or case.get("signature_verified") is not True
            or case.get("audit_record_count") != 2
            or case.get("unsigned_record_count") != 0
        ):
            raise RuntimeError(f"{expected_mode} signed audit proof is invalid")
        if case.get("provider_request_ids") != []:
            raise RuntimeError(f"{expected_mode} contains provider identity")
        if case.get("cost_event_ids") != []:
            raise RuntimeError(f"{expected_mode} contains cost identity")
        if (
            case.get("priced_call_count") != 0
            or case.get("cost_event_count") != 0
            or case.get("total_cost_usd") != 0.0
            or case.get("cost_identity_state") != "not_applicable_no_priced_call"
            or case.get("reconciliation_state") != "reconciled_zero_activity"
        ):
            raise RuntimeError(f"{expected_mode} zero-activity economics are invalid")
        total_cost += float(case["total_cost_usd"])
        refresh = _mapping(case.get("refresh"), label=f"{expected_mode} refresh")
        if (
            refresh.get("before_run_id") != run_id
            or refresh.get("restored_run_id") != run_id
            or refresh.get("restored_status") != "failed"
        ):
            raise RuntimeError(f"{expected_mode} refresh persistence is invalid")

    if len(set(run_ids)) != len(run_ids):
        raise RuntimeError("provider-fault cases reused a run identity")
    restore = _mapping(summary.get("d012_restore"), label="D-012 restore")
    if restore.get("exact") is not True or restore.get("before") != restore.get("after"):
        raise RuntimeError("D-012 was not restored exactly")
    return {
        "modes": list(EXPECTED_MODES),
        "run_ids": run_ids,
        "provider_calls_performed": 0,
        "total_cost_usd": total_cost,
        "d012_restored": True,
    }
