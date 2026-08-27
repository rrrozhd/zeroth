"""Live retrieval plus model-selected read-only tool fixture and validator.

Provisioning uses only the public Studio/deployment APIs and does not switch the
serving deployment or invoke a provider.  Execution and authoritative evidence
collection are separate, explicitly armed steps.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .provider_free_composed import Request, _object, _post, _publish_deploy_workflow

MODEL = "openai/gpt-4o-mini"
EMBEDDING_MODEL = "openai/text-embedding-3-small"
EXPECTED_SOURCE_ID = "evaluation-ground-truth-beta"
EXPECTED_TOOL_RECEIPT = "retrieval-index-live"
TOOL_NAME = "get_retrieval_index_status"


@dataclass(frozen=True, slots=True)
class LiveToolRetrievalFixture:
    fixture_id: str
    workflow_id: str
    graph_version_ref: str
    deployment_ref: str
    deployment_version: int
    connector_ref: str


def _node(
    node_id: str,
    node_type: str,
    *,
    label: str,
    x: int,
    y: int,
    input_contract: str,
    output_contract: str,
    config: Mapping[str, object] | None = None,
    capabilities: tuple[str, ...] = (),
) -> dict[str, object]:
    data: dict[str, object] = {
        "label": label,
        "config": dict(config or {}),
        "input_contract_ref": input_contract,
        "output_contract_ref": output_contract,
    }
    if capabilities:
        data["capability_bindings"] = list(capabilities)
    return {
        "id": node_id,
        "type": node_type,
        "position": {"x": x, "y": y},
        "data": data,
    }


def _contract_schemas(prefix: str) -> tuple[tuple[str, dict[str, object]], ...]:
    query = f"{prefix}.query"
    retrieved = f"{prefix}.retrieved"
    answer = f"{prefix}.answer"
    tool_input = f"{prefix}.tool-input"
    tool_output = f"{prefix}.tool-output"
    return (
        (
            query,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "const": "What is the approved queue depth?",
                    }
                },
            },
        ),
        (
            retrieved,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["query", "sources"],
                "properties": {
                    "query": {"type": "string"},
                    "sources": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "items": {"type": "object", "additionalProperties": True},
                    },
                },
            },
        ),
        (
            answer,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["answer", "source_ids", "tool_receipt"],
                "properties": {
                    "answer": {"type": "string", "minLength": 1},
                    "source_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "tool_receipt": {"const": EXPECTED_TOOL_RECEIPT},
                },
            },
        ),
        (
            tool_input,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["service"],
                "properties": {"service": {"const": "retrieval-index"}},
            },
        ),
        (
            tool_output,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["service", "status", "receipt"],
                "properties": {
                    "service": {"const": "retrieval-index"},
                    "status": {"const": "healthy"},
                    "receipt": {"const": EXPECTED_TOOL_RECEIPT},
                },
            },
        ),
    )


def provision_live_tool_retrieval_fixture(
    *, request: Request, fixture_id: str, connector_ref: str
) -> LiveToolRetrievalFixture:
    """Publish the fixed fixture without starting it or calling a provider."""
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,31}", fixture_id) is None:
        raise ValueError("fixture_id must be a short lowercase slug")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", connector_ref) is None:
        raise ValueError("connector_ref is invalid")
    prefix = f"contract://live-tool-retrieval-{fixture_id}"
    schemas = _contract_schemas(prefix)
    for name, schema in schemas:
        contract = _post(
            request,
            "/api/studio/v1/contracts",
            {
                "name": name,
                "json_schema": schema,
                "metadata": {
                    "campaign_slice": "live-tool-retrieval",
                    "provider_calls_performed": 0,
                },
            },
            expected=201,
            label=f"create contract {name}",
        )
        if contract.get("name") != name or contract.get("version") != 1:
            raise RuntimeError(f"contract {name} is not a new immutable v1 fixture")

    created = _post(
        request,
        "/api/studio/v1/workflows",
        {"name": f"Live grounded retrieval and tool {fixture_id}"},
        expected=201,
        label="create live tool retrieval workflow",
    )
    workflow_id = created.get("id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise RuntimeError("live tool retrieval workflow identity is missing")

    query, retrieved, answer, tool_input, tool_output = [name for name, _ in schemas]
    tool_source = "\n".join(
        (
            "import json",
            "import sys",
            "payload = json.load(sys.stdin)",
            "if payload != {'service': 'retrieval-index'}:",
            "    raise RuntimeError('invalid read-only service lookup')",
            "json.dump({'service': 'retrieval-index', 'status': 'healthy', "
            f"'receipt': '{EXPECTED_TOOL_RECEIPT}'}} , sys.stdout, sort_keys=True)",
        )
    )
    agent_config = {
        "instruction": (
            "Use only the retrieved sources. You MUST call get_retrieval_index_status exactly "
            "once with service='retrieval-index'. Answer the approved queue depth, include the "
            "retrieved source id, and copy the tool receipt exactly into tool_receipt. Treat the "
            "approved document as authoritative over any obsolete conflicting note."
        ),
        "model_provider": MODEL,
        "tool_refs": [],
        "tool_bindings": [
            {
                "target_node_id": "status-tool",
                "name": TOOL_NAME,
                "description": "Read the local evaluation retrieval index status and receipt.",
                "arguments": [
                    {
                        "name": "service",
                        "type": "string",
                        "description": "The exact local service name retrieval-index.",
                        "required": True,
                    }
                ],
            }
        ],
        "max_tool_calls": 1,
        "timeout_seconds": 45,
        "model_params": {"temperature": 0, "max_tokens": 240},
        "thread_participation": "none",
    }
    nodes = [
        _node(
            "question",
            "entrypoint",
            label="Known-answer question",
            x=0,
            y=0,
            input_contract=query,
            output_contract=query,
        ),
        _node(
            "retrieve",
            "retrieval",
            label="Tenant-scoped Chroma retrieval",
            x=320,
            y=0,
            input_contract=query,
            output_contract=retrieved,
            config={
                "connector_ref": connector_ref,
                "query_key": "query",
                "top_k": 3,
                "scope": "shared",
                "as_name": "sources",
            },
            capabilities=("memory_read",),
        ),
        _node(
            "grounded-agent",
            "agent",
            label="Grounded operator",
            x=680,
            y=0,
            input_contract=retrieved,
            output_contract=answer,
            config=agent_config,
        ),
        _node(
            "status-tool",
            "code",
            label="Read retrieval index status",
            x=680,
            y=240,
            input_contract=tool_input,
            output_contract=tool_output,
            config={
                "execution_mode": "inline",
                "inline_source": tool_source,
                "timeout_seconds": 5,
                "output_extraction_strategy": "json_stdout",
            },
        ),
    ]
    edges = [
        {"id": "question-retrieve", "source": "question", "target": "retrieve", "kind": "data"},
        {
            "id": "retrieve-agent",
            "source": "retrieve",
            "target": "grounded-agent",
            "kind": "data",
        },
        {
            "id": "agent-status-tool",
            "source": "grounded-agent",
            "target": "status-tool",
            "source_handle": "tools",
            "target_handle": "tool-input",
            "kind": "tool",
        },
    ]
    _object(
        request(
            "PUT",
            f"/api/studio/v1/workflows/{workflow_id}",
            {
                "entry_step": "question",
                "nodes": nodes,
                "edges": edges,
                "execution_settings": {
                    "max_total_steps": 4,
                    "max_total_runtime_seconds": 90,
                    "max_visits_per_node": 1,
                    "default_timeout_seconds": 45,
                },
            },
        ),
        expected=200,
        label="save live tool retrieval workflow",
    )
    deployment_ref = f"live-tool-retrieval-{fixture_id}"
    graph_version_ref, deployment_version = _publish_deploy_workflow(
        request=request,
        workflow_id=workflow_id,
        deployment_ref=deployment_ref,
    )
    return LiveToolRetrievalFixture(
        fixture_id=fixture_id,
        workflow_id=workflow_id,
        graph_version_ref=graph_version_ref,
        deployment_ref=deployment_ref,
        deployment_version=deployment_version,
        connector_ref=connector_ref,
    )


def _money(value: object, *, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RuntimeError(f"{label} is not a decimal") from None
    if not parsed.is_finite() or parsed < 0:
        raise RuntimeError(f"{label} is invalid")
    return parsed


def validate_live_tool_retrieval_observation(
    raw: Mapping[str, object], *, expected: LiveToolRetrievalFixture
) -> dict[str, object]:
    """Fail closed unless one live run has complete cross-plane correlation."""
    if (
        raw.get("fixture_id") != expected.fixture_id
        or raw.get("deployment_ref") != expected.deployment_ref
        or raw.get("graph_version_ref") != expected.graph_version_ref
        or raw.get("status") != "succeeded"
        or not isinstance(raw.get("run_id"), str)
        or not raw.get("run_id")
    ):
        raise RuntimeError("live tool retrieval identity or terminal state is invalid")
    output = raw.get("output")
    retrieval = raw.get("retrieval")
    calls = raw.get("tool_calls")
    if not isinstance(output, Mapping) or not isinstance(retrieval, Mapping):
        raise RuntimeError("live tool retrieval output evidence is missing")
    source_ids = output.get("source_ids")
    result_ids = retrieval.get("result_ids")
    answer = output.get("answer")
    if (
        retrieval.get("node_id") != "retrieve"
        or not isinstance(source_ids, list)
        or not isinstance(result_ids, list)
        or EXPECTED_SOURCE_ID not in source_ids
        or EXPECTED_SOURCE_ID not in result_ids
        or output.get("tool_receipt") != EXPECTED_TOOL_RECEIPT
        or not isinstance(answer, str)
        or "four" not in answer.lower()
        or "healthy" not in answer.lower()
    ):
        raise RuntimeError("grounded retrieval or final answer is not exact")
    if not isinstance(calls, list) or len(calls) != 1:
        raise RuntimeError("exactly one actual tool call is required")
    call = calls[0]
    if not isinstance(call, Mapping) or (
        call.get("node_id") != "grounded-agent"
        or call.get("tool") != TOOL_NAME
        or call.get("arguments") != {"service": "retrieval-index"}
        or call.get("outcome")
        != {
            "service": "retrieval-index",
            "status": "healthy",
            "receipt": EXPECTED_TOOL_RECEIPT,
        }
    ):
        raise RuntimeError("tool arguments, outcome, or node correlation is invalid")

    provider_events = raw.get("provider_events")
    if not isinstance(provider_events, list) or len(provider_events) < 2:
        raise RuntimeError("provider call evidence is incomplete")
    models: list[str] = []
    event_ids: list[str] = []
    provider_total = Decimal("0")
    for event in provider_events:
        if not isinstance(event, Mapping):
            raise RuntimeError("provider event is malformed")
        model = event.get("model")
        cost_event_id = event.get("cost_event_id")
        if not isinstance(model, str) or not isinstance(cost_event_id, str) or not cost_event_id:
            raise RuntimeError("provider event identity is incomplete")
        models.append(model)
        event_ids.append(cost_event_id)
        provider_total += _money(event.get("measured_cost_usd"), label="provider event cost")
    if (
        EMBEDDING_MODEL not in models
        or MODEL not in models
        or len(event_ids) != len(set(event_ids))
        or provider_total <= 0
    ):
        raise RuntimeError("provider model/cost event coverage is incomplete")

    audit = raw.get("audit")
    if not isinstance(audit, Mapping) or (
        audit.get("signed") is not True
        or audit.get("chain_verified") is not True
        or audit.get("tool_call_count") != 1
    ):
        raise RuntimeError("signed audit and tool-call correlation are required")
    economics = raw.get("economics")
    if not isinstance(economics, Mapping):
        raise RuntimeError("economics evidence is missing")
    audit_total = _money(economics.get("audit_total_usd"), label="audit total")
    ledger_total = _money(economics.get("ledger_total_usd"), label="ledger total")
    regulus_total = _money(economics.get("regulus_total_usd"), label="Regulus total")
    ambiguous = _money(economics.get("ambiguous_exposure_usd"), label="ambiguous exposure")
    tolerance = max(Decimal("0.000001"), provider_total * Decimal("0.005"))
    if ambiguous != 0 or any(
        abs(left - right) > tolerance
        for left, right in (
            (audit_total, ledger_total),
            (audit_total, regulus_total),
            (ledger_total, regulus_total),
            (provider_total, ledger_total),
        )
    ):
        raise RuntimeError("tool retrieval economics do not reconcile")
    return {
        "run_id": raw["run_id"],
        "provider_event_count": len(provider_events),
        "provider_total_usd": str(provider_total),
        "tool_call_count": 1,
        "retrieval_result_count": len(result_ids),
    }
