# Agents

## What it is

The **agent runtime** (`zeroth.core.agent_runtime`) is the layer that turns an `AgentNode` configuration into an actual LLM call: prompt assembly, provider invocation, tool attachment, output validation, retries, and thread state. It is what the [orchestrator](orchestrator.md) dispatches to when it hits an agent step.

## Why it exists

Calling an LLM from application code is deceptively hard: you have to assemble system/user messages from config, bind declared tools to real implementations, validate structured outputs against a contract, retry on transient failures, honor timeouts, persist thread history, and keep an auditable record of every prompt and response. The agent runtime consolidates all of that behind a single `AgentRunner.run()` call, so graph authors only describe *what* the agent should do and operators get consistent governance across every model and provider.

## Where it fits

Agents are invoked by the [orchestrator](orchestrator.md) once per `AgentNode` visit. The runner reads an `AgentConfig` derived from the node's `AgentNodeData`, assembles a prompt via `PromptAssembler`, calls a `ProviderAdapter` (litellm, GovernAI, or deterministic), validates the output with `OutputValidator` against the node's output contract, and hands an `AgentRunResult` back to the orchestrator. Tools declared on the node are resolved through `ToolAttachmentRegistry`; thread state is persisted through `RepositoryThreadStateStore`. Memory, secrets, and policy hooks plug in through the runtime context.

## Key types

- **`AgentRunner`** — the runtime entry point; `await runner.run(input_payload, thread_id=..., runtime_context=...)`.
- **`AgentConfig`** — resolved, runnable form of an `AgentNodeData` (prompt, model, tools, retry policy).
- **`ProviderAdapter`** — pluggable interface with `LiteLLMProviderAdapter`, `GovernedLLMProviderAdapter`, and `DeterministicProviderAdapter` implementations.
- **`PromptAssembler` / `PromptAssembly`** — builds the final message list from config plus runtime input.
- **`ToolAttachmentRegistry`** — binds declared tool refs to concrete callables, with permission enforcement.

## See also

- [Usage Guide: agents](../how-to/agents.md)
- [Concept: orchestrator](./orchestrator.md)
- [Concept: execution units](./execution-units.md)
