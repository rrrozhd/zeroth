# Agents: usage guide

## Overview

This guide shows how to configure and run an agent using `zeroth.runtime.agents` — the layer described in the [agents concept page](../concepts/agents.md). Agents are the LLM-powered nodes in a graph; the runtime handles prompt assembly, provider invocation, tool binding, output validation, retries, and thread state. You normally create an `AgentRunner` per deployment and hand it to the orchestrator, which calls `runner.run()` each time an `AgentNode` is visited.

## Minimal example

```python
import asyncio

from zeroth.runtime.agents import (
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

## Extraction contracts and required domain fields

An extraction contract should represent what the source text actually supports. If a
field can be absent from the text, make absence an explicit wire state even when the
application's final domain model requires a value:

```python
from typing import Literal

from pydantic import BaseModel, model_validator


class ExtractedField(BaseModel):
    status: Literal["explicit", "missing"]
    value: str | None
    source: str | None

    @model_validator(mode="after")
    def require_evidence_for_resolved_values(self) -> "ExtractedField":
        if self.status == "explicit" and (self.value is None or self.source is None):
            raise ValueError("resolved fields require a value and source evidence")
        return self


class TravelIntentExtraction(BaseModel):
    departure_window: ExtractedField


class TravelIntent(BaseModel):
    departure_window: str
```

`departure_window={"status": "missing", "value": null, "source": null}` is a
valid extraction result: the model did not invent a value. Convert to `TravelIntent`
only after the application has obtained the missing value; its required field then
enforces completeness at the domain boundary.

A contradictory resolved state, such as `status="explicit"` with `value=null`,
remains invalid. `LiteLLMProviderAdapter` reports that payload as
`AgentOutputValidationError`, preserving the model validator's details. If configured,
`RetryPolicy.retry_on_validation_error` can make a bounded second extraction attempt.
The SDK does not silently change the status, fill in a value, or return a partial model
that has bypassed the declared Pydantic contract. Transport and provider failures remain
`AgentProviderError`.

## Pitfalls

1. **Calling a provider without credentials** — `LiteLLMProviderAdapter` will raise `AgentProviderError`; gate your example with an env check like `examples/01_first_graph.py` does.
2. **Unvalidated model output** — skipping `OutputValidator` lets malformed LLM responses propagate into downstream nodes, where they will fail mapping or contract checks far away from the cause.
3. **Thread state leakage** — reusing the same `thread_id` across unrelated runs bleeds history between them; mint a fresh thread per logical conversation.
4. **Declaring tools the runtime can't bind** — undeclared refs raise `UndeclaredToolError`. Register every tool in the `ToolAttachmentRegistry` before starting the run.
5. **Infinite retry loops** — always set a finite `max_attempts` in `RetryPolicy`; `AgentRetryExhaustedError` is far easier to debug than a stuck run.

## Reference cross-link

See the [Python API reference for `zeroth.runtime.agents`](../reference/python-api/agents.md).
