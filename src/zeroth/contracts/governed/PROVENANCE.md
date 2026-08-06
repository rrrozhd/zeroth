# Provenance: vendored `governai`

This package is a surgical vendor of the `governai` framework, absorbed from the
published PyPI release **`governai==0.2.3`** on **2026-07-13**. Zeroth is the sole
product; the standalone `governai` repo and its satellite consumers were archived,
so the slice zeroth actually used was brought in-tree under `zeroth.core.governed`.

## What was vendored (18 modules — the transitive closure of zeroth's usage)

- `models/` — `common` (JSONValue, RunStatus, EventType, END_STEP…), `run_state`
  (RunState), `approval`, `audit`.
- `memory/` — `models` (MemoryEntry, MemoryScope), `connector` (MemoryConnector
  protocol), `scoped` (ScopedMemoryConnector), `auditing` (AuditingMemoryConnector).
- `audit/` — `emitter` (AuditEmitter), `redis` (RedisAuditEmitter).
- `tools/` — `base` (Tool, ExecutionPlacement), `python_tool` (PythonTool,
  PythonHandler), `manifest`, `cli_tool`.
- `app/spec.py` — GovernedStepSpec, GovernedFlowSpec, TransitionSpec, branch/end/
  route_to/then.
- `integrations/tool_calls.py` — extract_tool_calls, NormalizedToolCall,
  build_tool_message (langchain-core imported lazily).
- `runtime/` — `run_store` (RedisRunStore), `interrupts` (RedisInterruptStore).

## What was intentionally left behind (governai's execution kernel)

`runtime/local`, `workflows/*`, `approvals/engine`, `policies/*`, `agents/*`,
`execution/*`, `skills/*`, `sandbox`, `extensions`, `app/dsl`, `app/config`,
`app/flow`, `integrations/llm` (GovernedLLM). Zeroth never used these — it runs its
own orchestrator (`zeroth.core.orchestrator`), sandbox (`execution_units`), run
store (`runs.repository`), and LLM path (`agent_runtime.LiteLLMProviderAdapter`).

## Modifications from upstream

Internal `governai.*` imports were rewritten to `zeroth.core.governed.*`. No logic
changes — this is a move, not a rewrite; behavior parity is proved by the full test
suite and `tests/memory/test_governai_shared_contract.py` (now an internal
invariant: `ScopedMemoryConnector` must resolve `SHARED → "__shared__"`, which the
tenant-isolation wrapper depends on).

Upstream license retained in `LICENSE`.
