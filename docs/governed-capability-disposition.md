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
- **Governance (Task 13)** — moved to `zeroth.governance.audit.*` when the
  governance packages moved and `core/governed/audit` was consolidated with
  `core/audit`; the legacy module republishes the same object.
- **Runtime (Task 14)** — moved into the maintained runtime packages when
  the agent runtime consolidated; the legacy module republishes the same
  object.
- **Contracts (Task 14)** — definition moved to `zeroth.contracts.governed.*`
  in Task 14; the legacy module republishes the same object.
- **Integrations (Task 15)** — stays in place until the memory and
  integration packages consolidate under `zeroth.integrations`; consumed by
  the runtime through excepted edges documented in
  `src/zeroth/_architecture.py`.

## Top-level aggregator (`zeroth.core.governed`)

| Symbol | Defined in | Disposition |
| --- | --- | --- |
| `RunState` | `models/run_state.py` | Contracts (Task 12) — definition moves to `zeroth.contracts.governed.models.run_state`; aggregator keeps republishing |
| `RunStatus` | `models/common.py` | Contracts (Task 12) — definition moves to `zeroth.contracts.governed.models.common`; aggregator keeps republishing |
| `Tool` | `tools/base.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.base`; aggregator keeps republishing |

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

## `audit/` — audit emitters → Governance (Task 13)

| Symbol | Module | Disposition |
| --- | --- | --- |
| `AuditEmitter` | `audit/emitter.py` | Governance (Task 13) → `zeroth.governance.audit.emitter`, consolidated with `core/audit` under `governance/audit` |
| `emit_event` | `audit/emitter.py` | Governance (Task 13) → `zeroth.governance.audit.emitter` |
| `RedisAuditEmitter` | `audit/redis.py` | Governance (Task 13) → `zeroth.governance.audit.redis`; its only concrete dependency is the optional third-party `redis.asyncio` client, imported lazily, so no zeroth-internal edge leaves the governance domain |

## `integrations/` — provider tool-call helpers → Integrations (Task 15)

| Symbol | Module | Disposition |
| --- | --- | --- |
| `NormalizedToolCall` | `integrations/tool_calls.py` | Integrations (Task 15) |
| `extract_tool_calls` | `integrations/tool_calls.py` | Integrations (Task 15) |
| `build_tool_message` | `integrations/tool_calls.py` | Integrations (Task 15) |
| `GovernedToolCallLoop` | `integrations/tool_calls.py` | Integrations (Task 15) |

## `memory/` — memory type system and connector wrappers → Integrations (Task 15), except the contract-owned scope enum

| Symbol | Module | Disposition |
| --- | --- | --- |
| `AuditingMemoryConnector` | `memory/auditing.py` | Integrations (Task 15) — moves with the `core/memory` consolidation |
| `MemoryConnector` | `memory/connector.py` | Integrations (Task 15) — moves with the `core/memory` consolidation |
| `MemoryScope` | `memory/models.py` | Contracts (Task 14) → `zeroth.contracts.governed.models.memory`; the legacy module republishes it |
| `MemoryEntry` | `memory/models.py` | Integrations (Task 15) — moves with the `core/memory` consolidation |
| `ScopedMemoryConnector` | `memory/scoped.py` | Integrations (Task 15) — moves with the `core/memory` consolidation |

## `runtime/` — interrupt and run stores → Runtime (Task 14)

| Symbol | Module | Disposition |
| --- | --- | --- |
| `InterruptRequest` | `runtime/interrupts.py` | Runtime (Task 14) → `zeroth.runtime.orchestration.interrupts` |
| `InterruptResolution` | `runtime/interrupts.py` | Runtime (Task 14) → `zeroth.runtime.orchestration.interrupts` |
| `InterruptStore` | `runtime/interrupts.py` | Runtime (Task 14) → `zeroth.runtime.orchestration.interrupts` |
| `InMemoryInterruptStore` | `runtime/interrupts.py` | Runtime (Task 14) → `zeroth.runtime.orchestration.interrupts` |
| `RedisInterruptStore` | `runtime/interrupts.py` | Runtime (Task 14) → `zeroth.runtime.orchestration.interrupts` |
| `InterruptManager` | `runtime/interrupts.py` | Runtime (Task 14) → `zeroth.runtime.orchestration.interrupts` |
| `StateConcurrencyError` | `runtime/run_store.py` | Runtime (Task 14) → `zeroth.runtime.orchestration.run_store` |
| `ThreadAwareRunStore` | `runtime/run_store.py` | Runtime (Task 14) → `zeroth.runtime.orchestration.run_store` |
| `RunStore` | `runtime/run_store.py` | Runtime (Task 14) → `zeroth.runtime.orchestration.run_store` |
| `InMemoryRunStore` | `runtime/run_store.py` | Runtime (Task 14) → `zeroth.runtime.orchestration.run_store` |
| `RedisRunStore` | `runtime/run_store.py` | Runtime (Task 14) → `zeroth.runtime.orchestration.run_store` |

## `tools/` — tool primitives → Runtime (Task 14)

| Symbol | Module | Disposition |
| --- | --- | --- |
| `InModelT` | `tools/base.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.base` |
| `OutModelT` | `tools/base.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.base` |
| `ToolError` | `tools/base.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.base` |
| `ToolValidationError` | `tools/base.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.base` |
| `ToolExecutionError` | `tools/base.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.base` |
| `CLIToolError` | `tools/base.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.base` |
| `CLIToolProcessError` | `tools/base.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.base` |
| `CLIToolOutputError` | `tools/base.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.base` |
| `CLIToolTimeoutError` | `tools/base.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.base` |
| `Tool` | `tools/base.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.base` |
| `CLITool` | `tools/cli_tool.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.cli_tool` |
| `ToolManifest` | `tools/manifest.py` | Runtime (Task 14) → `zeroth.runtime.agents.tooling.manifest` |
| `PythonReturn` | `tools/python_tool.py` | Runtime (Task 14) → `zeroth.runtime.agents.toolingthon_tool` |
| `PythonHandler` | `tools/python_tool.py` | Runtime (Task 14) → `zeroth.runtime.agents.toolingthon_tool` |
| `PythonTool` | `tools/python_tool.py` | Runtime (Task 14) → `zeroth.runtime.agents.toolingthon_tool` |
| `tool` | `tools/python_tool.py` | Runtime (Task 14) → `zeroth.runtime.agents.toolingthon_tool` |

Note: `ExecutionPlacement` formerly lived in `tools/base.py`; Task 12's
registry row moved it to `zeroth.contracts.registry.tooling`, with
`tools/base.py` re-importing it (see the symbol migration log in
`docs/backend-import-migration.md`).
