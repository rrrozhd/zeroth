"""Tests for the RetrievalNode dispatch path (RAG-01, RAG-03).

The fake connector proves node *wiring* (query_key -> query text, top_k -> limit,
output shape, audit source attribution) — not semantic ranking, which lives in the
real vector connectors and is out of scope here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from zeroth.core.governed.memory.models import MemoryEntry, MemoryScope

from zeroth.core.audit import AuditRepository
from zeroth.core.execution_units import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.contracts.graph import ExecutionSettings, Graph, RetrievalNode, RetrievalNodeData
from zeroth.core.memory.models import ConnectorManifest
from zeroth.core.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.core.orchestrator.runtime import NodeDispatcherError
from zeroth.core.runs import RunRepository, RunStatus


class _RecordingConnector:
    """Records search queries and returns canned entries (no real similarity)."""

    def __init__(self, entries: list[MemoryEntry]) -> None:
        self._entries = entries
        self.search_calls: list[dict] = []

    async def search(self, query, scope, *, target=None):  # noqa: ANN001
        self.search_calls.append(dict(query))
        return list(self._entries)

    async def write(self, key, value, scope, *, target=None):  # noqa: ANN001 - protocol completeness
        pass

    async def read(self, key, scope, *, target=None):  # noqa: ANN001
        return None

    async def delete(self, key, scope, *, target=None):  # noqa: ANN001
        pass


def _entries() -> list[MemoryEntry]:
    return [
        MemoryEntry(
            key="doc-a#0",
            value="zeroth is a governed agent platform",
            scope=MemoryScope.SHARED,
            scope_target="",
            metadata={"source_id": "doc-a"},
        ),
        MemoryEntry(
            key="doc-b#3",
            value="agents run as API services",
            scope=MemoryScope.SHARED,
            scope_target="",
            metadata={"source_id": "doc-b"},
        ),
    ]


def _resolver(connector: _RecordingConnector) -> MemoryConnectorResolver:
    registry = InMemoryConnectorRegistry()
    registry.register(
        "docs",
        ConnectorManifest(connector_type="chroma", scope=MemoryScope.SHARED),
        connector,
    )
    return MemoryConnectorResolver(registry=registry, workflow_name="test-wf")


def _node(**kwargs) -> RetrievalNode:
    return RetrievalNode(
        node_id="retrieve",
        graph_version_ref="g:v1",
        retrieval=RetrievalNodeData(connector_ref="docs", **kwargs),
    )


def _orchestrator(resolver, *, run_repository=None, audit_repository=None) -> RuntimeOrchestrator:
    return RuntimeOrchestrator(
        run_repository=run_repository or MagicMock(),
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        memory_resolver=resolver,
        audit_repository=audit_repository,
    )


def _run_stub() -> MagicMock:
    run = MagicMock()
    run.run_id = "r1"
    run.thread_id = ""
    run.tenant_id = "default"  # WS-B: memory resolution is fail-closed on tenant
    return run


@pytest.mark.asyncio
async def test_retrieval_dispatch_wires_query_topk_output_and_audit() -> None:
    connector = _RecordingConnector(_entries())
    orch = _orchestrator(_resolver(connector))
    node = _node(query_key="question", top_k=3, as_name="context")

    output, audit = await orch._dispatch_retrieval_node(
        node, _run_stub(), {"question": "what is zeroth?"}
    )

    # query_key drives the query text; top_k reaches search as `limit`
    assert connector.search_calls == [{"text": "what is zeroth?", "limit": 3}]
    # output augments the input with retrieved chunks under as_name
    assert output["question"] == "what is zeroth?"
    assert output["context"][0] == {
        "id": "doc-a#0",
        "content": "zeroth is a governed agent platform",
        "metadata": {"source_id": "doc-a"},
    }
    # RAG-03: audit carries query + per-chunk source attribution (ids + metadata)...
    retrieval_audit = audit["retrieval"]
    assert retrieval_audit["query"] == "what is zeroth?"
    assert retrieval_audit["result_count"] == 2
    assert retrieval_audit["sources"] == [
        {"id": "doc-a#0", "metadata": {"source_id": "doc-a"}},
        {"id": "doc-b#3", "metadata": {"source_id": "doc-b"}},
    ]
    # ...but NOT the chunk bodies (avoid dumping content into the governance log)
    assert "governed agent platform" not in str(retrieval_audit)


@pytest.mark.asyncio
async def test_retrieval_missing_query_key_raises() -> None:
    orch = _orchestrator(_resolver(_RecordingConnector(_entries())))
    with pytest.raises(NodeDispatcherError, match="must be a non-empty string"):
        await orch._dispatch_retrieval_node(
            _node(query_key="question"), _run_stub(), {"other": "x"}
        )


@pytest.mark.asyncio
async def test_retrieval_unknown_connector_raises() -> None:
    empty_resolver = MemoryConnectorResolver(
        registry=InMemoryConnectorRegistry(), workflow_name="test-wf"
    )
    orch = _orchestrator(empty_resolver)
    with pytest.raises(NodeDispatcherError, match="unknown memory connector"):
        await orch._dispatch_retrieval_node(_node(query_key="q"), _run_stub(), {"q": "hi"})


@pytest.mark.asyncio
async def test_retrieval_node_runs_end_to_end_through_orchestrator(sqlite_db) -> None:
    connector = _RecordingConnector(_entries())
    orch = _orchestrator(_resolver(connector), run_repository=RunRepository(sqlite_db))
    graph = Graph(
        graph_id="g-rag",
        name="rag",
        entry_step="retrieve",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[_node(query_key="question", as_name="context")],
        edges=[],
    )

    run = await orch.run_graph(graph, {"question": "what is zeroth?"})

    assert run.status is RunStatus.COMPLETED
    assert connector.search_calls[0]["text"] == "what is zeroth?"  # dispatch reached the node
    assert run.final_output is not None and "context" in run.final_output


@pytest.mark.asyncio
async def test_retrieval_audit_record_is_persisted_with_sources(sqlite_db) -> None:
    # RAG-03 end-to-end: the retrieval audit lands in a persisted NodeAuditRecord,
    # not just the value returned by the dispatch method.
    connector = _RecordingConnector(_entries())
    orch = _orchestrator(
        _resolver(connector),
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
    )
    graph = Graph(
        graph_id="g-rag-audit",
        name="rag-audit",
        entry_step="retrieve",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[_node(query_key="question", as_name="context")],
        edges=[],
    )

    run = await orch.run_graph(graph, {"question": "what is zeroth?"})
    assert run.status is RunStatus.COMPLETED

    audits = await AuditRepository(sqlite_db).list_by_run(run.run_id)
    retrieve_audits = [a for a in audits if a.node_id == "retrieve"]
    assert len(retrieve_audits) == 1
    sources = retrieve_audits[0].execution_metadata["retrieval"]["sources"]
    assert [s["id"] for s in sources] == ["doc-a#0", "doc-b#3"]
