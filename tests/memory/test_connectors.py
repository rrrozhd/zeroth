from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.agent_runtime import AgentConfig, AgentRunner
from zeroth.agent_runtime.provider import CallableProviderAdapter, ProviderResponse
from zeroth.memory import (
    ConnectorManifest,
    ConnectorScope,
    InMemoryConnectorRegistry,
    KeyValueMemoryConnector,
    MemoryConnectorResolver,
    RunEphemeralMemoryConnector,
    ThreadMemoryConnector,
)
from zeroth.runs import ThreadRepository


class MemoryInput(BaseModel):
    value: int


class MemoryOutput(BaseModel):
    value: int
    seen: int = 0


def _runner(registry: InMemoryConnectorRegistry, *, thread_repository=None) -> AgentRunner:
    resolver = MemoryConnectorResolver(registry=registry, thread_repository=thread_repository)
    return AgentRunner(
        AgentConfig(
            name="memory-agent",
            instruction="remember",
            model_name="governai:test",
            input_model=MemoryInput,
            output_model=MemoryOutput,
            memory_refs=["memory://shared"],
        ),
        CallableProviderAdapter(
            lambda request: ProviderResponse(
                content={
                    "value": request.metadata["input_payload"]["value"],
                    "seen": request.metadata["runtime_context"]
                    .get("memory", {})
                    .get("memory://shared", {})
                    .get("latest", {})
                    .get("value", 0),
                }
            )
        ),
        memory_resolver=resolver,
    )


@pytest.mark.asyncio
async def test_shared_memory_instance_is_visible_across_agents(sqlite_db) -> None:
    registry = InMemoryConnectorRegistry()
    registry.register(
        "memory://shared",
        ConnectorManifest(
            connector_type="key_value",
            scope=ConnectorScope.SHARED,
            instance_id="shared-instance",
        ),
        KeyValueMemoryConnector(),
    )
    runner = _runner(registry, thread_repository=ThreadRepository(sqlite_db))

    first = await runner.run({"value": 3}, runtime_context={"run_id": "run-1"})
    second = await runner.run({"value": 7}, runtime_context={"run_id": "run-1"})

    assert first.output_data["seen"] == 0
    assert second.output_data["seen"] == 3
    assert second.audit_record["extra"]["memory_interactions"][0]["operation"] == "read"
    assert second.audit_record["extra"]["memory_interactions"][1]["operation"] == "write"


@pytest.mark.asyncio
async def test_ephemeral_memory_isolated_per_run() -> None:
    registry = InMemoryConnectorRegistry()
    registry.register(
        "memory://shared",
        ConnectorManifest(
            connector_type="ephemeral",
            scope=ConnectorScope.RUN,
        ),
        RunEphemeralMemoryConnector(),
    )
    runner = _runner(registry)

    first = await runner.run({"value": 5}, runtime_context={"run_id": "run-a"})
    second = await runner.run({"value": 8}, runtime_context={"run_id": "run-b"})

    assert first.output_data["seen"] == 0
    assert second.output_data["seen"] == 0


@pytest.mark.asyncio
async def test_thread_memory_persists_across_runs_and_updates_thread_bindings(sqlite_db) -> None:
    registry = InMemoryConnectorRegistry()
    registry.register(
        "memory://shared",
        ConnectorManifest(
            connector_type="thread",
            scope=ConnectorScope.THREAD,
        ),
        ThreadMemoryConnector(),
    )
    thread_repository = ThreadRepository(sqlite_db)
    thread_repository.resolve(
        "thread-1",
        graph_version_ref="graph:v1",
        deployment_ref="graph",
    )
    runner = _runner(registry, thread_repository=thread_repository)

    await runner.run({"value": 2}, thread_id="thread-1", runtime_context={"run_id": "run-1"})
    second = await runner.run(
        {"value": 4}, thread_id="thread-1", runtime_context={"run_id": "run-2"}
    )

    thread = thread_repository.get("thread-1")
    assert second.output_data["seen"] == 2
    assert thread is not None
    assert thread.memory_bindings[0].connector_id == "memory://shared"
