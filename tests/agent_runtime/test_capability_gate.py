"""WS-C: the AgentRunner tool-call and memory capability gates."""

from __future__ import annotations

import pytest
from zeroth.core.governed.memory.models import MemoryScope
from pydantic import BaseModel

from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    ProviderResponse,
    ToolAttachmentManifest,
)
from zeroth.core.agent_runtime.mcp import MCPServerConfig
from zeroth.core.memory.connectors import KeyValueMemoryConnector
from zeroth.core.memory.models import ConnectorManifest
from zeroth.core.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver
from zeroth.core.policy import Capability, CapabilityDeniedError


class DemoInput(BaseModel):
    query: str


class DemoOutput(BaseModel):
    answer: str
    score: int


def _tool_config(required: tuple[Capability, ...]) -> AgentConfig:
    return AgentConfig(
        name="agent",
        instruction="Use tools.",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        tool_attachments=[
            ToolAttachmentManifest(
                alias="search",
                executable_unit_ref="node://search",
                required_capabilities=required,
            )
        ],
    )


def _tool_provider() -> DeterministicProviderAdapter:
    return DeterministicProviderAdapter(
        [
            ProviderResponse(
                content=None,
                tool_calls=[{"id": "t1", "name": "search", "args": {"query": "hi"}}],
            ),
            ProviderResponse(content='{"answer":"done","score":1}'),
        ]
    )


@pytest.mark.asyncio
async def test_tool_denied_when_capability_not_granted() -> None:
    invoked: list[str] = []

    async def executor(binding, arguments):  # noqa: ANN001
        invoked.append(binding.alias)
        return {"ok": True}

    runner = AgentRunner(
        _tool_config((Capability.NETWORK_WRITE,)), _tool_provider(), tool_executor=executor
    )
    result = await runner.run(
        {"query": "hi"},
        enforcement_context={
            "capability_enforcement_active": True,
            "effective_capabilities": [],  # granted nothing
        },
    )
    # Executor was provably NOT invoked; the denial was fed back to the model.
    assert invoked == []
    tool_audit = result.audit_record["extra"]["tool_calls"][0]
    assert tool_audit["error"] is not None
    assert "capability denied" in tool_audit["error"]


@pytest.mark.asyncio
async def test_tool_allowed_when_capability_granted() -> None:
    invoked: list[str] = []

    async def executor(binding, arguments):  # noqa: ANN001
        invoked.append(binding.alias)
        return {"ok": True}

    runner = AgentRunner(
        _tool_config((Capability.NETWORK_WRITE,)), _tool_provider(), tool_executor=executor
    )
    result = await runner.run(
        {"query": "hi"},
        enforcement_context={
            "capability_enforcement_active": True,
            "effective_capabilities": ["network_write"],
        },
    )
    assert invoked == ["search"]
    assert result.output_data == {"answer": "done", "score": 1}


@pytest.mark.asyncio
async def test_tool_fail_closed_when_enforcement_active_but_context_empty() -> None:
    invoked: list[str] = []

    async def executor(binding, arguments):  # noqa: ANN001
        invoked.append(binding.alias)
        return {"ok": True}

    runner = AgentRunner(
        _tool_config((Capability.SECRET_ACCESS,)), _tool_provider(), tool_executor=executor
    )
    # Enforcement active but no effective_capabilities key at all -> empty grant.
    result = await runner.run(
        {"query": "hi"},
        enforcement_context={"capability_enforcement_active": True},
    )
    assert invoked == []
    assert "capability denied" in result.audit_record["extra"]["tool_calls"][0]["error"]


@pytest.mark.asyncio
async def test_tool_ungated_when_enforcement_inactive() -> None:
    invoked: list[str] = []

    async def executor(binding, arguments):  # noqa: ANN001
        invoked.append(binding.alias)
        return {"ok": True}

    runner = AgentRunner(
        _tool_config((Capability.SECRET_ACCESS,)), _tool_provider(), tool_executor=executor
    )
    # No enforcement context (guard unwired) -> tool runs, no gate.
    result = await runner.run({"query": "hi"})
    assert invoked == ["search"]
    assert result.output_data == {"answer": "done", "score": 1}


# --- memory gate through the runner ----------------------------------------


def _memory_config() -> AgentConfig:
    return AgentConfig(
        name="agent",
        instruction="answer",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        memory_refs=["memory://kv"],
    )


def _memory_resolver() -> MemoryConnectorResolver:
    reg = InMemoryConnectorRegistry()
    reg.register(
        "memory://kv",
        ConnectorManifest(connector_type="key_value", scope=MemoryScope.RUN, instance_id="i"),
        KeyValueMemoryConnector(),
    )
    return MemoryConnectorResolver(registry=reg)


@pytest.mark.asyncio
async def test_runner_memory_denied_without_memory_read() -> None:
    provider = DeterministicProviderAdapter([ProviderResponse(content='{"answer":"a","score":1}')])
    runner = AgentRunner(_memory_config(), provider, memory_resolver=_memory_resolver())
    with pytest.raises(Exception) as exc:  # noqa: PT011 - CapabilityDeniedError subclasses PermissionError
        await runner.run(
            {"query": "hi"},
            runtime_context={"tenant_id": "default"},
            enforcement_context={
                "capability_enforcement_active": True,
                "effective_capabilities": [],
            },
        )
    assert "capability denied" in str(exc.value)


@pytest.mark.asyncio
async def test_runner_memory_allowed_with_read_and_write() -> None:
    provider = DeterministicProviderAdapter([ProviderResponse(content='{"answer":"a","score":1}')])
    runner = AgentRunner(_memory_config(), provider, memory_resolver=_memory_resolver())
    result = await runner.run(
        {"query": "hi"},
        runtime_context={"tenant_id": "default"},
        enforcement_context={
            "capability_enforcement_active": True,
            "effective_capabilities": ["memory_read", "memory_write"],
        },
    )
    assert result.output_data == {"answer": "a", "score": 1}


def _mcp_config() -> AgentConfig:
    return AgentConfig(
        name="agent",
        instruction="Use MCP tools.",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        mcp_servers=[MCPServerConfig(name="files", command="mcp-files")],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "granted",
    [[], ["process_spawn"], ["external_api_call"]],
)
async def test_mcp_startup_denied_before_any_process_spawn(monkeypatch, granted) -> None:
    """A partial grant must deny BEFORE MCPClientManager exists — no subprocess,
    no connection, no discovery."""
    constructions: list[object] = []

    def _record_construction(self, configs) -> None:
        constructions.append(configs)

    monkeypatch.setattr(
        "zeroth.core.agent_runtime.runner.MCPClientManager.__init__", _record_construction
    )
    provider = DeterministicProviderAdapter([ProviderResponse(content='{"answer":"a","score":1}')])
    runner = AgentRunner(_mcp_config(), provider)
    with pytest.raises(CapabilityDeniedError):
        await runner.run(
            {"query": "hi"},
            enforcement_context={
                "capability_enforcement_active": True,
                "effective_capabilities": granted,
            },
        )
    assert constructions == []


@pytest.mark.asyncio
async def test_mcp_startup_allowed_with_both_capabilities(monkeypatch) -> None:
    async def _fake_start(self):  # noqa: ANN001
        return []

    async def _fake_stop(self):  # noqa: ANN001
        return None

    monkeypatch.setattr("zeroth.core.agent_runtime.runner.MCPClientManager.start", _fake_start)
    monkeypatch.setattr("zeroth.core.agent_runtime.runner.MCPClientManager.stop", _fake_stop)
    provider = DeterministicProviderAdapter([ProviderResponse(content='{"answer":"a","score":1}')])
    runner = AgentRunner(_mcp_config(), provider)
    result = await runner.run(
        {"query": "hi"},
        enforcement_context={
            "capability_enforcement_active": True,
            "effective_capabilities": ["process_spawn", "external_api_call"],
        },
    )
    assert result.output_data == {"answer": "a", "score": 1}


@pytest.mark.asyncio
async def test_mcp_startup_ungated_when_enforcement_inactive(monkeypatch) -> None:
    started: list[str] = []

    async def _fake_start(self):  # noqa: ANN001
        started.append("started")
        return []

    async def _fake_stop(self):  # noqa: ANN001
        return None

    monkeypatch.setattr("zeroth.core.agent_runtime.runner.MCPClientManager.start", _fake_start)
    monkeypatch.setattr("zeroth.core.agent_runtime.runner.MCPClientManager.stop", _fake_stop)
    provider = DeterministicProviderAdapter([ProviderResponse(content='{"answer":"a","score":1}')])
    runner = AgentRunner(_mcp_config(), provider)
    result = await runner.run({"query": "hi"})
    assert started == ["started"]
    assert result.output_data == {"answer": "a", "score": 1}
