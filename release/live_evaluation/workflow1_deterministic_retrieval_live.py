"""Provider-free Workflow-1 Chroma negative fixtures and validation.

The fixture uses two tenant-qualified Chroma connector configurations backed by
the same pinned loopback service: one exact empty collection and one exact
two-document conflicting corpus. Retrieval runs through the product retrieval
node. A local inline-code node performs deterministic abstention/conflict
reasoning, so no chat or embedding provider is reachable.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from zeroth.integrations.memory.chroma_connector import (
    LOCAL_HASH_EMBEDDING_MODEL,
    ChromaDBMemoryConnector,
    local_hash_embedding,
)
from zeroth.integrations.memory.governed.models import MemoryScope
from zeroth.integrations.memory.tenant_scoped import tenant_slug

from .provider_free_composed import Request, _object, _post, _publish_deploy_workflow

CRITERIA = (
    "workflow1.negative-no-result",
    "workflow1.negative-conflicting-document",
)

CONFLICT_DOCUMENTS = (
    (
        "approved-queue-depth-four",
        "Synthetic approved operations note: the queue depth is four.",
        "approved",
    ),
    (
        "obsolete-queue-depth-six",
        "Synthetic obsolete operations note: the queue depth is six.",
        "obsolete",
    ),
)


@dataclass(frozen=True, slots=True)
class DeterministicRetrievalFixture:
    schema_version: int
    fixture_id: str
    workflow_id: str
    graph_version_ref: str
    deployment_ref: str
    deployment_version: int
    empty_connector_ref: str
    conflict_connector_ref: str
    empty_collection: str
    conflict_collection: str
    corpus_hashes: tuple[str, ...]
    provider_calls_performed: int = 0


def _sha256(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _node(
    node_id: str,
    node_type: str,
    *,
    label: str,
    x: int,
    y: int,
    contract: str,
    config: Mapping[str, object] | None = None,
    capabilities: tuple[str, ...] = (),
) -> dict[str, object]:
    data: dict[str, object] = {
        "label": label,
        "config": dict(config or {}),
        "input_contract_ref": contract,
        "output_contract_ref": contract,
    }
    if capabilities:
        data["capability_bindings"] = list(capabilities)
    return {"id": node_id, "type": node_type, "position": {"x": x, "y": y}, "data": data}


def configure_local_chroma_fixtures(
    *,
    request: Request,
    chroma_client: Any,
    tenant_id: str,
    fixture_id: str,
) -> tuple[str, str, str, str, tuple[str, ...]]:
    """Persist two local connector configs and converge their collections exactly."""
    prefix = re.sub(r"[^a-z0-9]+", "_", f"w1_negative_{fixture_id}").strip("_")
    empty_ref = f"w1-empty-{fixture_id}"
    conflict_ref = f"w1-conflict-{fixture_id}"
    connector_rows = (
        (empty_ref, f"{prefix}_empty"),
        (conflict_ref, f"{prefix}_conflict"),
    )
    for ref, collection_prefix in connector_rows:
        response = request(
            "POST",
            "/v1/connectors",
            {
                "ref": ref,
                "backend_type": "chroma",
                "params": {
                    "host": "chroma",
                    "port": 8000,
                    "collection_prefix": collection_prefix,
                    "embedding_model": LOCAL_HASH_EMBEDDING_MODEL,
                },
            },
        )
        _object(response, expected=201, label=f"create {ref}")

    target = f"{tenant_slug(tenant_id)}____shared__"
    empty_connector = ChromaDBMemoryConnector(
        chroma_client,
        collection_prefix=connector_rows[0][1],
        embedding_model=LOCAL_HASH_EMBEDDING_MODEL,
    )
    conflict_connector = ChromaDBMemoryConnector(
        chroma_client,
        collection_prefix=connector_rows[1][1],
        embedding_model=LOCAL_HASH_EMBEDDING_MODEL,
    )
    empty_name = empty_connector._collection_name(MemoryScope.SHARED, target)
    conflict_name = conflict_connector._collection_name(MemoryScope.SHARED, target)
    empty = chroma_client.get_or_create_collection(empty_name, metadata={"hnsw:space": "cosine"})
    existing = empty.get().get("ids", [])
    if existing:
        empty.delete(ids=list(existing))
    conflict = chroma_client.get_or_create_collection(
        conflict_name, metadata={"hnsw:space": "cosine"}
    )
    conflict_existing = conflict.get().get("ids", [])
    if conflict_existing:
        conflict.delete(ids=list(conflict_existing))
    ids = [row[0] for row in CONFLICT_DOCUMENTS]
    contents = [row[1] for row in CONFLICT_DOCUMENTS]
    conflict.upsert(
        ids=ids,
        documents=[json.dumps(content) for content in contents],
        embeddings=[
            local_hash_embedding(f"{doc_id}: {json.dumps(content)}")
            for doc_id, content, _ in CONFLICT_DOCUMENTS
        ],
        metadatas=[
            {"key": doc_id, "scope": "shared", "target": target, "authority": authority}
            for doc_id, _, authority in CONFLICT_DOCUMENTS
        ],
    )
    if empty.count() != 0 or set(conflict.get().get("ids", [])) != set(ids):
        raise RuntimeError("tenant-scoped Chroma fixtures did not converge exactly")
    return (
        empty_ref,
        conflict_ref,
        empty_name,
        conflict_name,
        tuple(_sha256(item) for item in contents),
    )


def provision_fixture(
    *,
    request: Request,
    chroma_client: Any,
    tenant_id: str,
    fixture_id: str,
) -> DeterministicRetrievalFixture:
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,31}", fixture_id) is None:
        raise ValueError("fixture_id must be a short lowercase slug")
    empty_ref, conflict_ref, empty_name, conflict_name, hashes = configure_local_chroma_fixtures(
        request=request,
        chroma_client=chroma_client,
        tenant_id=tenant_id,
        fixture_id=fixture_id,
    )
    contract = f"contract://provider-free-w1-retrieval-negatives-{fixture_id}"
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["scenario", "query"],
        "properties": {
            "scenario": {"type": "string", "enum": ["no_result", "conflict"]},
            "query": {"type": "string", "minLength": 1},
            "context": {"type": "array"},
            "answer": {"type": "string"},
            "source_ids": {"type": "array", "items": {"type": "string"}},
            "zeroth_if": {"type": "object"},
        },
    }
    created_contract = _post(
        request,
        "/api/studio/v1/contracts",
        {
            "name": contract,
            "json_schema": schema,
            "metadata": {
                "campaign_slice": "workflow1-deterministic-chroma-negatives",
                "provider_calls_performed": 0,
                "embedding_model": LOCAL_HASH_EMBEDDING_MODEL,
            },
        },
        expected=201,
        label="create retrieval-negative contract",
    )
    if created_contract.get("version") != 1:
        raise RuntimeError("retrieval-negative contract is not immutable v1")
    created = _post(
        request,
        "/api/studio/v1/workflows",
        {"name": f"Workflow 1 deterministic Chroma negatives {fixture_id}"},
        expected=201,
        label="create retrieval-negative workflow",
    )
    workflow_id = created.get("id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise RuntimeError("retrieval-negative workflow identity is missing")
    reasoning = "\n".join(
        (
            "import json",
            "import sys",
            "payload = json.load(sys.stdin)",
            "context = payload.get('context') or []",
            "ids = sorted(str(item.get('id')) for item in context)",
            "scenario = payload['scenario']",
            "if scenario == 'no_result':",
            "    if context: raise RuntimeError('expected an empty tenant corpus')",
            "    payload['answer'] = 'No grounded result found in the tenant-scoped corpus.'",
            "elif scenario == 'conflict':",
            "    if ids != ['approved-queue-depth-four', 'obsolete-queue-depth-six']:",
            "        raise RuntimeError('expected both conflicting sources')",
            "    payload['answer'] = "
            "'Conflict detected: approved and obsolete documents disagree.'",
            "else: raise RuntimeError('unknown scenario')",
            "payload['source_ids'] = ids",
            "payload.pop('context', None)",
            "payload.pop('zeroth_if', None)",
            "json.dump(payload, sys.stdout, sort_keys=True)",
        )
    )
    nodes = [
        _node("request", "entrypoint", label="Grounded question", x=0, y=0, contract=contract),
        _node(
            "scenario",
            "if",
            label="No-result or conflict?",
            x=260,
            y=0,
            contract=contract,
            config={"expression": "payload.scenario == 'no_result'"},
        ),
        _node(
            "empty-retrieval",
            "retrieval",
            label="Search empty tenant corpus",
            x=560,
            y=-150,
            contract=contract,
            config={
                "connector_ref": empty_ref,
                "query_key": "query",
                "top_k": 2,
                "scope": "shared",
                "as_name": "context",
            },
            capabilities=("memory_read",),
        ),
        _node(
            "conflict-retrieval",
            "retrieval",
            label="Search conflicting tenant corpus",
            x=560,
            y=150,
            contract=contract,
            config={
                "connector_ref": conflict_ref,
                "query_key": "query",
                "top_k": 2,
                "scope": "shared",
                "as_name": "context",
            },
            capabilities=("memory_read",),
        ),
        _node(
            "reason-empty",
            "code",
            label="Deterministic empty-corpus decision",
            x=900,
            y=-150,
            contract=contract,
            config={
                "execution_mode": "inline",
                "inline_source": reasoning,
                "timeout_seconds": 5,
                "output_extraction_strategy": "json_stdout",
            },
        ),
        _node(
            "reason-conflict",
            "code",
            label="Deterministic conflict decision",
            x=900,
            y=150,
            contract=contract,
            config={
                "execution_mode": "inline",
                "inline_source": reasoning,
                "timeout_seconds": 5,
                "output_extraction_strategy": "json_stdout",
            },
        ),
    ]
    edges = [
        {"id": "request-scenario", "source": "request", "target": "scenario"},
        {
            "id": "scenario-empty",
            "source": "scenario",
            "target": "empty-retrieval",
            "source_handle": "true",
        },
        {
            "id": "scenario-conflict",
            "source": "scenario",
            "target": "conflict-retrieval",
            "source_handle": "false",
        },
        {"id": "empty-reason", "source": "empty-retrieval", "target": "reason-empty"},
        {
            "id": "conflict-reason",
            "source": "conflict-retrieval",
            "target": "reason-conflict",
        },
    ]
    _object(
        request(
            "PUT",
            f"/api/studio/v1/workflows/{workflow_id}",
            {
                "entry_step": "request",
                "nodes": nodes,
                "edges": edges,
                "execution_settings": {
                    "max_total_steps": 8,
                    "max_total_runtime_seconds": 30,
                    "max_visits_per_node": 2,
                    "default_timeout_seconds": 10,
                },
            },
        ),
        expected=200,
        label="save retrieval-negative workflow",
    )
    deployment_ref = f"provider-free-w1-chroma-negatives-{fixture_id}"
    graph_ref, deployment_version = _publish_deploy_workflow(
        request=request,
        workflow_id=workflow_id,
        deployment_ref=deployment_ref,
    )
    return DeterministicRetrievalFixture(
        schema_version=1,
        fixture_id=fixture_id,
        workflow_id=workflow_id,
        graph_version_ref=graph_ref,
        deployment_ref=deployment_ref,
        deployment_version=deployment_version,
        empty_connector_ref=empty_ref,
        conflict_connector_ref=conflict_ref,
        empty_collection=empty_name,
        conflict_collection=conflict_name,
        corpus_hashes=hashes,
    )
