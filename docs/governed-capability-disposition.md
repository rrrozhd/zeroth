# Governed capability disposition

The `zeroth.core.governed` package is the vendored subset of `governai==0.2.3`
(see `src/zeroth/core/governed/PROVENANCE.md`). Task 12 moves only its
**contract** slice — the flow/step specifications and the shared state models —
to `zeroth.contracts.governed`. Everything else in the bundle is inventoried
here so no symbol is orphaned, but the implementation subpackages are
**deliberately not classified yet**: audit consolidation is Task 13's
disposition work, and the runtime/tools/memory/integrations implementations
are Task 14's. A later task may not delete or relocate any symbol below
without updating its row.

Legend for the Disposition column:

- **Contracts (Task 12)** — moved to `zeroth.contracts.governed.*` in this
  task; the legacy module republishes the same object.
- **Deferred (Task 13)** — stays in place; classified when governance
  packages move and `core/governed/audit` is consolidated with `core/audit`.
- **Deferred (Task 14)** — stays in place; classified when the runtime and
  integration implementations move behind owned protocols.

## Top-level aggregator (`zeroth.core.governed`)

| Symbol | Defined in | Disposition |
| --- | --- | --- |
| `RunState` | `models/run_state.py` | Contracts (Task 12) — definition moves to `zeroth.contracts.governed.models.run_state`; aggregator keeps republishing |
| `RunStatus` | `models/common.py` | Contracts (Task 12) — definition moves to `zeroth.contracts.governed.models.common`; aggregator keeps republishing |
| `Tool` | `tools/base.py` | Deferred (Task 14) — runtime-owned tool primitive; aggregator keeps republishing |

## `app/` — flow and step specifications → Contracts (Task 12)

| Symbol | Module | Disposition |
| --- | --- | --- |
| `TransitionSpec` | `app/spec.py` | Contracts (Task 12) → `zeroth.contracts.governed.app.spec` |
| `GovernedStepSpec` | `app/spec.py` | Contracts (Task 12) → `zeroth.contracts.governed.app.spec` |
| `InterruptContract` | `app/spec.py` | Contracts (Task 12) → `zeroth.contracts.governed.app.spec` |
| `ChannelSpec` | `app/spec.py` | Contracts (Task 12) → `zeroth.contracts.governed.app.spec` |
| `GovernedFlowSpec` | `app/spec.py` | Contracts (Task 12) → `zeroth.contracts.governed.app.spec` |
| `then` | `app/spec.py` | Contracts (Task 12) → `zeroth.contracts.governed.app.spec` |
| `end` | `app/spec.py` | Contracts (Task 12) → `zeroth.contracts.governed.app.spec` |
| `branch` | `app/spec.py` | Contracts (Task 12) → `zeroth.contracts.governed.app.spec` |
| `route_to` | `app/spec.py` | Contracts (Task 12) → `zeroth.contracts.governed.app.spec` |

## `models/` — shared state models → Contracts (Task 12)

| Symbol | Module | Disposition |
| --- | --- | --- |
| `ApprovalDecisionType` | `models/approval.py` | Contracts (Task 12) → `zeroth.contracts.governed.models.approval` |
| `ApprovalRequest` | `models/approval.py` | Contracts (Task 12) → `zeroth.contracts.governed.models.approval` |
| `ApprovalDecision` | `models/approval.py` | Contracts (Task 12) → `zeroth.contracts.governed.models.approval` |
| `AuditExtension` | `models/audit.py` | Contracts (Task 12) → `zeroth.contracts.governed.models.audit` |
| `AuditEvent` | `models/audit.py` | Contracts (Task 12) → `zeroth.contracts.governed.models.audit` |
| `JSONValue` | `models/common.py` | Contracts (Task 12) → `zeroth.contracts.governed.models.common` |
| `END_STEP` | `models/common.py` | Contracts (Task 12) → `zeroth.contracts.governed.models.common` |
| `RunStatus` | `models/common.py` | Contracts (Task 12) → `zeroth.contracts.governed.models.common` |
| `DeterminismMode` | `models/common.py` | Contracts (Task 12) → `zeroth.contracts.governed.models.common` |
| `EventType` | `models/common.py` | Contracts (Task 12) → `zeroth.contracts.governed.models.common` |
| `normalize_step_ref` | `models/common.py` | Contracts (Task 12) → `zeroth.contracts.governed.models.common` |
| `RunState` | `models/run_state.py` | Contracts (Task 12) → `zeroth.contracts.governed.models.run_state` |

## `audit/` — audit emitters → Deferred (Task 13)

| Symbol | Module | Disposition |
| --- | --- | --- |
| `AuditEmitter` | `audit/emitter.py` | Deferred (Task 13) — consolidated with `core/audit` under `governance/audit` |
| `emit_event` | `audit/emitter.py` | Deferred (Task 13) |
| `RedisAuditEmitter` | `audit/redis.py` | Deferred (Task 13) |

## `integrations/` — provider tool-call helpers → Deferred (Task 14)

| Symbol | Module | Disposition |
| --- | --- | --- |
| `NormalizedToolCall` | `integrations/tool_calls.py` | Deferred (Task 14) |
| `extract_tool_calls` | `integrations/tool_calls.py` | Deferred (Task 14) |
| `build_tool_message` | `integrations/tool_calls.py` | Deferred (Task 14) |
| `GovernedToolCallLoop` | `integrations/tool_calls.py` | Deferred (Task 14) |

## `memory/` — memory type system and connector wrappers → Deferred (Task 14)

| Symbol | Module | Disposition |
| --- | --- | --- |
| `AuditingMemoryConnector` | `memory/auditing.py` | Deferred (Task 14) |
| `MemoryConnector` | `memory/connector.py` | Deferred (Task 14) |
| `MemoryScope` | `memory/models.py` | Deferred (Task 14) — see the `zeroth.runtime.orchestration.dispatcher` exception reason in `src/zeroth/_architecture.py` |
| `MemoryEntry` | `memory/models.py` | Deferred (Task 14) |
| `ScopedMemoryConnector` | `memory/scoped.py` | Deferred (Task 14) |

## `runtime/` — interrupt and run stores → Deferred (Task 14)

| Symbol | Module | Disposition |
| --- | --- | --- |
| `InterruptRequest` | `runtime/interrupts.py` | Deferred (Task 14) |
| `InterruptResolution` | `runtime/interrupts.py` | Deferred (Task 14) |
| `InterruptStore` | `runtime/interrupts.py` | Deferred (Task 14) |
| `InMemoryInterruptStore` | `runtime/interrupts.py` | Deferred (Task 14) |
| `RedisInterruptStore` | `runtime/interrupts.py` | Deferred (Task 14) |
| `InterruptManager` | `runtime/interrupts.py` | Deferred (Task 14) |
| `StateConcurrencyError` | `runtime/run_store.py` | Deferred (Task 14) |
| `ThreadAwareRunStore` | `runtime/run_store.py` | Deferred (Task 14) |
| `RunStore` | `runtime/run_store.py` | Deferred (Task 14) |
| `InMemoryRunStore` | `runtime/run_store.py` | Deferred (Task 14) |
| `RedisRunStore` | `runtime/run_store.py` | Deferred (Task 14) |

## `tools/` — tool primitives → Deferred (Task 14)

| Symbol | Module | Disposition |
| --- | --- | --- |
| `InModelT` | `tools/base.py` | Deferred (Task 14) |
| `OutModelT` | `tools/base.py` | Deferred (Task 14) |
| `ToolError` | `tools/base.py` | Deferred (Task 14) |
| `ToolValidationError` | `tools/base.py` | Deferred (Task 14) |
| `ToolExecutionError` | `tools/base.py` | Deferred (Task 14) |
| `CLIToolError` | `tools/base.py` | Deferred (Task 14) |
| `CLIToolProcessError` | `tools/base.py` | Deferred (Task 14) |
| `CLIToolOutputError` | `tools/base.py` | Deferred (Task 14) |
| `CLIToolTimeoutError` | `tools/base.py` | Deferred (Task 14) |
| `Tool` | `tools/base.py` | Deferred (Task 14) |
| `CLITool` | `tools/cli_tool.py` | Deferred (Task 14) |
| `ToolManifest` | `tools/manifest.py` | Deferred (Task 14) |
| `PythonReturn` | `tools/python_tool.py` | Deferred (Task 14) |
| `PythonHandler` | `tools/python_tool.py` | Deferred (Task 14) |
| `PythonTool` | `tools/python_tool.py` | Deferred (Task 14) |
| `tool` | `tools/python_tool.py` | Deferred (Task 14) |

Note: `ExecutionPlacement` formerly lived in `tools/base.py`; Task 12's
registry row moved it to `zeroth.contracts.registry.tooling`, with
`tools/base.py` re-importing it (see the symbol migration log in
`docs/backend-import-migration.md`).
