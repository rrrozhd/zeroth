from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.agent_runtime import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    ProviderResponse,
    RepositoryThreadResolver,
    RepositoryThreadStateStore,
    RetryPolicy,
    ToolAttachmentBridge,
    ToolAttachmentManifest,
    ToolAttachmentRegistry,
)
from zeroth.execution_units import (
    ExecutableUnitBinding,
    ExecutableUnitRegistry,
    ExecutableUnitRunner,
    ExecutionMode,
    InputMode,
    NativeUnitManifest,
    OutputMode,
    PythonModuleArtifactSource,
)
from zeroth.runs import RunRepository, ThreadRepository


class AgentInput(BaseModel):
    query: str


class AgentOutput(BaseModel):
    answer: str
    score: int


class ToolInput(BaseModel):
    query: str


class ToolOutput(BaseModel):
    result: str


async def _tool_handler(_ctx, data: ToolInput) -> dict[str, str]:  # noqa: ANN001
    return {"result": data.query.upper()}


@pytest.mark.asyncio
async def test_repository_thread_state_store_integrates_with_agent_runner(sqlite_db) -> None:
    thread_repository = ThreadRepository(sqlite_db)
    run_repository = RunRepository(sqlite_db)
    resolver = RepositoryThreadResolver(thread_repository)
    resolved = await resolver.resolve(
        None,
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        run_id="run-a",
    )
    store = RepositoryThreadStateStore(
        sqlite_db,
        run_repository=run_repository,
        thread_repository=thread_repository,
    )
    provider = DeterministicProviderAdapter([ProviderResponse(content='{"answer":"ok","score":1}')])
    runner = AgentRunner(
        AgentConfig(
            name="threaded",
            instruction="Return a valid answer.",
            model_name="governai:test",
            input_model=AgentInput,
            output_model=AgentOutput,
        ),
        provider,
        thread_state_store=store,
    )

    result = await runner.run({"query": "hello"}, thread_id=resolved.thread.thread_id)

    assert result.output_data == {"answer": "ok", "score": 1}
    latest = await store.load(resolved.thread.thread_id)
    assert latest is not None
    assert latest["output"] == {"answer": "ok", "score": 1}


@pytest.mark.asyncio
async def test_agent_runner_executes_governai_style_tool_calls_through_executable_units() -> None:
    manifest = NativeUnitManifest(
        unit_id="search-tool",
        onboarding_mode=ExecutionMode.NATIVE,
        runtime="python",
        artifact_source=PythonModuleArtifactSource(ref="demo.tools:search"),
        callable_ref="demo.tools:search",
        entrypoint_type="python_callable",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://tool-input",
        output_contract_ref="contract://tool-output",
        cache_identity_fields={"python": "3.12"},
    )
    eu_registry = ExecutableUnitRegistry()
    eu_registry.register(
        ExecutableUnitBinding(
            manifest_ref="eu://search",
            manifest=manifest,
            input_model=ToolInput,
            output_model=ToolOutput,
            python_handler=_tool_handler,
        )
    )
    eu_runner = ExecutableUnitRunner(eu_registry)
    tool_registry = ToolAttachmentRegistry(
        [
            ToolAttachmentManifest(
                alias="search",
                executable_unit_ref="eu://search",
                permission_scope=("net:query",),
            )
        ]
    )
    provider = DeterministicProviderAdapter(
        [
            ProviderResponse(
                content=None,
                tool_calls=[{"id": "call-1", "name": "search", "args": {"query": "hello"}}],
            ),
            ProviderResponse(content='{"answer":"done","score":2}'),
        ]
    )
    runner = AgentRunner(
        AgentConfig(
            name="tool-agent",
            instruction="Use tools when needed.",
            model_name="governai:test",
            input_model=AgentInput,
            output_model=AgentOutput,
            allowed_tools=["search"],
            retry_policy=RetryPolicy(max_retries=0),
        ),
        provider,
        tool_bridge=ToolAttachmentBridge(tool_registry),
        tool_executor=lambda binding, payload: _execute_tool(
            eu_runner,
            binding.executable_unit_ref,
            payload,
        ),
        granted_tool_permissions=["net:query"],
    )

    result = await runner.run({"query": "hello"})

    assert result.output_data == {"answer": "done", "score": 2}
    assert result.tool_call_records[0]["tool"]["alias"] == "search"
    assert result.tool_call_records[0]["outcome"] == {"result": "HELLO"}


async def _execute_tool(
    eu_runner: ExecutableUnitRunner,
    manifest_ref: str,
    payload: dict[str, str],
) -> dict[str, str]:
    result = await eu_runner.run_manifest_ref(manifest_ref, payload)
    return result.output_data
