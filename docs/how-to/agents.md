# Agents: usage guide

## Overview

This guide shows how to configure and run an agent using `zeroth.core.agent_runtime` — the layer described in the [agents concept page](../concepts/agents.md). Agents are the LLM-powered nodes in a graph; the runtime handles prompt assembly, provider invocation, tool binding, output validation, retries, and thread state. You normally create an `AgentRunner` per deployment and hand it to the orchestrator, which calls `runner.run()` each time an `AgentNode` is visited.

## Minimal example

```python
import asyncio

from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    LiteLLMProviderAdapter,
    ModelParams,
    PromptConfig,
    PromptMessage,
)


async def main() -> None:
    config = AgentConfig(
        agent_id="greeter",
        prompt=PromptConfig(
            messages=[
                PromptMessage(role="system", content="You are a terse assistant."),
                PromptMessage(role="user", content="{{ message }}"),
            ],
        ),
        model_params=ModelParams(model="openai/gpt-4o-mini", temperature=0.0),
    )
    runner = AgentRunner(
        config=config,
        provider=LiteLLMProviderAdapter(),
    )
    result = await runner.run({"message": "Say hi in five words."}, thread_id=None)
    print(result.output_data)


asyncio.run(main())
```

## Common patterns

- **Provider swap** — use `DeterministicProviderAdapter` in tests (canned responses, no network) and `LiteLLMProviderAdapter` in dev/prod (OpenAI, Anthropic, local models via one interface).
- **Structured output** — declare an `output_contract_ref` on the agent node and register it; `OutputValidator` will coerce and validate the model response.
- **Retry policy** — set `RetryPolicy(max_attempts=..., backoff_seconds=...)` on the `AgentConfig` to survive transient provider errors.
- **Tool attachment** — bind callables to declared tool refs through `ToolAttachmentRegistry` so the model can only call explicitly declared tools.

## Pitfalls

1. **Calling a provider without credentials** — `LiteLLMProviderAdapter` will raise `AgentProviderError`; gate your example with an env check like `examples/01_first_graph.py` does.
2. **Unvalidated model output** — skipping `OutputValidator` lets malformed LLM responses propagate into downstream nodes, where they will fail mapping or contract checks far away from the cause.
3. **Thread state leakage** — reusing the same `thread_id` across unrelated runs bleeds history between them; mint a fresh thread per logical conversation.
4. **Declaring tools the runtime can't bind** — undeclared refs raise `UndeclaredToolError`. Register every tool in the `ToolAttachmentRegistry` before starting the run.
5. **Infinite retry loops** — always set a finite `max_attempts` in `RetryPolicy`; `AgentRetryExhaustedError` is far easier to debug than a stuck run.

## Reference cross-link

See the [Python API reference for `zeroth.core.agent_runtime`](../reference/python-api/agents.md).
