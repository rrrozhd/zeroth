"""WS-C: orchestrator-level capability enforcement.

Covers the two orchestrator-owned surfaces: the retrieval memory-resolve caller
(gated on MEMORY_READ) and the agent tool executor (agent-invoked units now run
under the calling agent's enforcement_context — the prior bypass is closed).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    AgentToolBinding,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
    RetrievalNode,
    RetrievalNodeData,
)
from zeroth.governance.policy import CapabilityDeniedError, PolicyGuard
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.integrations.memory.governed.models import MemoryEntry, MemoryScope
from zeroth.integrations.memory.models import ConnectorManifest
from zeroth.integrations.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver
from zeroth.runtime.orchestration import RuntimeOrchestrator

REF = "cap-orch@1"


class _Connector:
    async def search(self, query, scope, *, target=None):  # noqa: ANN001
        return [
            MemoryEntry(
                key="d#0", value="chunk", scope=MemoryScope.SHARED, scope_target="", metadata={}
            )
        ]

    async def read(self, key, scope, *, target=None):  # noqa: ANN001
        return None

    async def write(self, key, value, scope, *, target=None):  # noqa: ANN001
        pass

    async def delete(self, key, scope, *, target=None):  # noqa: ANN001
        pass


def _resolver() -> MemoryConnectorResolver:
    registry = InMemoryConnectorRegistry()
    registry.register(
        "docs", ConnectorManifest(connector_type="chroma", scope=MemoryScope.SHARED), _Connector()
    )
    return MemoryConnectorResolver(registry=registry)


def _run(metadata: dict[str, Any]) -> MagicMock:
    run = MagicMock()
    run.run_id = "r1"
    run.thread_id = ""
    run.tenant_id = "default"
    run.metadata = metadata
    return run


def _orch(resolver, *, policy_guard=None) -> RuntimeOrchestrator:
    return RuntimeOrchestrator(
        run_repository=MagicMock(),
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        memory_resolver=resolver,
        policy_guard=policy_guard,
    )


def _retrieval_node() -> RetrievalNode:
    return RetrievalNode(
        node_id="retrieve",
        graph_version_ref=REF,
        retrieval=RetrievalNodeData(connector_ref="docs", query_key="q"),
    )


@pytest.mark.asyncio
async def test_retrieval_denied_without_memory_read() -> None:
    orch = _orch(_resolver(), policy_guard=PolicyGuard())
    run = _run({"enforcement": {"retrieve": {"effective_capabilities": []}}})
    with pytest.raises(CapabilityDeniedError):
        await orch._dispatch_retrieval_node(_retrieval_node(), run, {"q": "hello"})


@pytest.mark.asyncio
async def test_retrieval_allowed_with_memory_read() -> None:
    orch = _orch(_resolver(), policy_guard=PolicyGuard())
    run = _run({"enforcement": {"retrieve": {"effective_capabilities": ["memory_read"]}}})
    output, _audit = await orch._dispatch_retrieval_node(_retrieval_node(), run, {"q": "hello"})
    assert output["retrieved"][0]["content"] == "chunk"


@pytest.mark.asyncio
async def test_retrieval_ungated_when_guard_unwired() -> None:
    orch = _orch(_resolver(), policy_guard=None)
    run = _run({})
    output, _audit = await orch._dispatch_retrieval_node(_retrieval_node(), run, {"q": "hello"})
    assert output["retrieved"][0]["content"] == "chunk"


# --- tool-executor bypass closed -------------------------------------------


class _RecordingUnitRunner:
    """Records the enforcement_context threaded into unit runs."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def run(self, manifest_ref, payload, *, enforcement_context=None):  # noqa: ANN001
        self.received.append(dict(enforcement_context or {}))
        return MagicMock(output_data={"echo": dict(payload)})


def _tool_graph() -> Graph:
    return Graph(
        graph_id="cap-orch",
        name="cap",
        version=1,
        entry_step="agent",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref=REF,
                input_contract_ref="contract://in",
                output_contract_ref="contract://out",
                agent=AgentNodeData(
                    instruction="use tool",
                    model_provider="governai:test",
                    tool_bindings=[
                        AgentToolBinding(target_node_id="unit", name="run_unit", description="d")
                    ],
                ),
            ),
            ExecutableUnitNode(
                node_id="unit",
                graph_version_ref=REF,
                input_contract_ref="contract://in",
                output_contract_ref="contract://out",
                executable_unit=ExecutableUnitNodeData(
                    execution_mode="native", manifest_ref="eu://unit"
                ),
            ),
        ],
        edges=[Edge(edge_id="te", source_node_id="agent", target_node_id="unit", kind="tool")],
    )


@pytest.mark.asyncio
async def test_tool_executor_threads_enforcement_context_to_unit() -> None:
    runner = _RecordingUnitRunner()
    orch = RuntimeOrchestrator(
        run_repository=MagicMock(),
        agent_runners={},
        executable_unit_runner=runner,
        policy_guard=PolicyGuard(),
    )
    enforcement = {
        "effective_capabilities": ["network_write"],
        "allowed_secrets": ["API_KEY"],
        "network_mode": "restricted",
    }
    executor = orch._tool_executor_for(_tool_graph(), enforcement)
    binding = MagicMock(executable_unit_ref="node://unit", alias="run_unit")

    result = await executor(binding, {"x": 1})

    assert result == {"echo": {"x": 1}}
    # The unit ran under the agent's enforcement envelope (bypass closed).
    assert runner.received == [enforcement]
