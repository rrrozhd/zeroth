from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, Field

from zeroth.agent_runtime import (
    AgentConfig,
    AgentOutputValidationError,
    AgentRunner,
    AgentTimeoutError,
    DeterministicProviderAdapter,
    InMemoryThreadStateStore,
    OutputValidator,
    PromptAssembler,
    PromptConfig,
    ProviderResponse,
    RetryPolicy,
)
from zeroth.agent_runtime.prompt import AgentAuditSerializer


class DemoInput(BaseModel):
    query: str
    secret: str


class DemoOutput(BaseModel):
    answer: str
    score: int = Field(ge=0)


class SlowProvider:
    async def ainvoke(self, request):  # noqa: ANN001
        await asyncio.sleep(0.05)
        return ProviderResponse(content='{"answer":"slow","score":1}')


@pytest.mark.asyncio
async def test_prompt_assembly_redacts_sensitive_values() -> None:
    config = AgentConfig(
        name="demo",
        description="demo agent",
        instruction="Answer precisely.",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        allowed_tools=["tool://search"],
        memory_refs=["memory://thread"],
        prompt_config=PromptConfig(redact_keys=("secret",), extra_context={"trace": True}),
    )

    assembly = PromptAssembler().assemble(
        config,
        DemoInput(query="hello", secret="top-secret"),
        thread_state={"secret": "thread-secret", "step": 1},
    )

    assert "Answer precisely." in assembly.rendered_prompt
    assert "top-secret" not in assembly.rendered_prompt
    assert "thread-secret" not in assembly.rendered_prompt
    assert assembly.metadata["tool_refs"] == ["tool://search"]


@pytest.mark.asyncio
async def test_agent_runner_validates_output_and_checkpoints_thread_state() -> None:
    config = AgentConfig(
        name="demo",
        instruction="Answer precisely.",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        retry_policy=RetryPolicy(max_retries=1),
    )
    provider = DeterministicProviderAdapter(
        [ProviderResponse(content='{"answer":"done","score":2}')]
    )
    store = InMemoryThreadStateStore()
    runner = AgentRunner(config, provider, thread_state_store=store)

    result = await runner.run(DemoInput(query="hello", secret="top-secret"), thread_id="thread-1")

    assert result.output_data == {"answer": "done", "score": 2}
    assert result.attempts == 1
    assert provider.requests[0].model_name == "governai:test"
    assert store.latest("thread-1") is not None
    assert store.latest("thread-1")["output"] == {"answer": "done", "score": 2}
    assert "top-secret" not in result.audit_record["prompt"]["rendered_prompt"]


@pytest.mark.asyncio
async def test_agent_runner_retries_on_validation_error_then_succeeds() -> None:
    config = AgentConfig(
        name="demo",
        instruction="Answer precisely.",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        retry_policy=RetryPolicy(max_retries=1),
    )
    provider = DeterministicProviderAdapter(
        [
            ProviderResponse(content='{"answer":"missing-score"}'),
            ProviderResponse(content='{"answer":"done","score":7}'),
        ]
    )
    runner = AgentRunner(config, provider)

    result = await runner.run({"query": "hello", "secret": "x"})

    assert result.output_data == {"answer": "done", "score": 7}
    assert result.attempts == 2
    with pytest.raises(AgentOutputValidationError):
        OutputValidator().validate(DemoOutput, ProviderResponse(content='{"answer":"bad"}'))


@pytest.mark.asyncio
async def test_agent_runner_times_out_and_retries() -> None:
    config = AgentConfig(
        name="demo",
        instruction="Answer precisely.",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        timeout_seconds=0.01,
        retry_policy=RetryPolicy(max_retries=1),
    )
    provider = SlowProvider()
    runner = AgentRunner(config, provider)

    with pytest.raises(AgentTimeoutError) as excinfo:
        await runner.run(DemoInput(query="hello", secret="top-secret"))

    assert "timed out" in str(excinfo.value)


@pytest.mark.asyncio
async def test_agent_audit_serializer_redacts_sensitive_metadata() -> None:
    config = AgentConfig(
        name="demo",
        instruction="Answer precisely.",
        model_name="governai:test",
        input_model=DemoInput,
        output_model=DemoOutput,
        prompt_config=PromptConfig(redact_keys=("secret",)),
    )
    prompt = PromptAssembler().assemble(
        config,
        DemoInput(query="hello", secret="top-secret"),
        runtime_context={"secret": "runtime-secret"},
    )
    serializer = AgentAuditSerializer(redact_keys={"secret"})

    record = serializer.serialize_prompt(prompt)

    assert "top-secret" not in record["rendered_prompt"]
    assert "runtime-secret" not in str(record)
