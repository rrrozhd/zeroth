# Backend Import Migration Guide

This guide is the change log for public Python import locations during the
backend architecture refactor. The baseline captured on 2026-07-18 has not
moved any symbols: all public imports still resolve from their legacy
locations. The canonical package shells exist so moves can proceed in focused,
independently verified slices.

## Compatibility policy

- A useful library capability may move to a clearer domain package, but its
  call signature, return behavior, and public exception semantics remain
  stable.
- A temporary re-export is optional. Consumers should migrate to the canonical
  import recorded here instead of relying on compatibility shims.
- `tests/contracts/fixtures/backend_surface_legacy.json` is immutable after
  the corrected baseline inventory is accepted. It identifies protected
  capabilities independently of their future import locations.
- `tests/contracts/fixtures/backend_surface_canonical.json` is evolving. Every
  edit to it must be committed separately from production moves and accompanied
  by a row in this guide.
- Superseded or removed symbols require explicit dead-code evidence covering
  static reachability, dynamic registration, exports, documentation, examples,
  service schemas, and optional integrations.

## Initial package dispositions

These rows establish the approved package-level destinations. A **move** row
does not claim that its public symbols have moved; the status remains
`Skeleton only` until a production slice and its separate canonical-surface
update are committed. **Unchanged** rows stay at their current paths unless a
separate design amendment approves a move.

| Current package | Canonical package | Disposition | Initial status |
| --- | --- | --- | --- |
| `zeroth.core.orchestrator`, `zeroth.core.agent_runtime`, `zeroth.core.parallel`, `zeroth.core.subgraph`, `zeroth.core.context_window` | `zeroth.runtime.orchestration`, `zeroth.runtime.agents`, `zeroth.runtime.parallel`, `zeroth.runtime.subgraphs`, `zeroth.runtime.context` | Move and decompose | Skeleton only |
| `zeroth.core.approvals`, `zeroth.core.audit`, `zeroth.core.identity`, `zeroth.core.policy`, `zeroth.core.guardrails`, `zeroth.core.retention` | `zeroth.governance.approvals`, `zeroth.governance.audit`, `zeroth.governance.identity`, `zeroth.governance.policy`, `zeroth.governance.guardrails`, `zeroth.governance.retention` | Move; decompose retention | Skeleton only |
| `zeroth.core.artifacts`, `zeroth.core.config`, `zeroth.core.dispatch`, `zeroth.core.observability`, `zeroth.core.secrets`, `zeroth.core.signing`, `zeroth.core.storage` | `zeroth.platform.artifacts`, `zeroth.platform.config`, `zeroth.platform.dispatch`, `zeroth.platform.observability`, `zeroth.platform.secrets`, `zeroth.platform.signing`, `zeroth.platform.storage` | Move; add shared persistence and primitives | Skeleton only |
| `zeroth.core.conditions`, `zeroth.core.contracts`, `zeroth.core.graph`, `zeroth.core.mappings`, `zeroth.core.templates` | `zeroth.contracts.conditions`, `zeroth.contracts.registry`, `zeroth.contracts.graph`, `zeroth.contracts.mappings`, `zeroth.contracts.templates` | Move; decompose graph validation | Skeleton only |
| `zeroth.core.runs` models and protocols | `zeroth.runtime.runs` | Move domain contracts | Canonical import path published |
| `zeroth.core.runs` SQL persistence | `zeroth.integrations.persistence.runs` | Move and decompose persistence adapters | Serialization and checkpoint storage extracted |
| `zeroth.core.service`, `zeroth.core.deployments`, `zeroth.core.webhooks` | `zeroth.service.api`, `zeroth.service.bootstrap`, `zeroth.service.deployments`, `zeroth.service.webhooks` | Move; decompose bootstrap | Skeleton only |
| `zeroth.core.econ`, `zeroth.econ_plane` | `zeroth.econ.analytics`, `zeroth.econ.instrumentation`, `zeroth.econ.plane` | Move and consolidate | Skeleton only |
| `zeroth.core.execution_units`, `zeroth.core.http`, `zeroth.core.memory`, `zeroth.core.rag`, `zeroth.core.sandbox_sidecar` | `zeroth.integrations.execution`, `zeroth.integrations.http`, `zeroth.integrations.memory`, `zeroth.integrations.rag`, `zeroth.integrations.sandbox` | Move; preserve optional integrations | Skeleton only |
| `zeroth.core.eval` | `zeroth.eval` | Move stable evaluation capability | Skeleton only |
| `zeroth.core.governed.app`, `zeroth.core.governed.models` | `zeroth.contracts.governed` | Move and consolidate specifications | Skeleton only |
| `zeroth.core.governed.runtime`, `zeroth.core.governed.tools` | `zeroth.runtime.orchestration`, `zeroth.runtime.agents` | Move into maintained runtime boundaries | Skeleton only |
| `zeroth.core.governed.audit`, `zeroth.core.governed.memory`, `zeroth.core.governed.integrations` | `zeroth.governance.audit`, `zeroth.integrations.memory`, relevant integration packages | Move only after capability inventory | Skeleton only |
| `zeroth.core.demos`, `zeroth.core.examples`, `zeroth.core.migrations`, `zeroth.econ_plane._migrations` | Existing locations | Unchanged | Unchanged by this refactor |
| `zeroth.core` package shell and top-level CLI/entry points | Existing locations during migration | Unchanged compatibility shell | Unchanged during staged moves |

## Symbol migration log

Entries use this exact schema and are added only with the separate
canonical-surface update that follows a verified production move.

| Old path and symbol | New path and symbol | Disposition | Compatibility status | Replacement | Removal evidence |
| --- | --- | --- | --- | --- | --- |
| `zeroth.core.runs:Run` | `zeroth.runtime.runs:Run` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:RunConditionResult` | `zeroth.runtime.runs:RunConditionResult` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:RunFailureState` | `zeroth.runtime.runs:RunFailureState` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:RunHistoryEntry` | `zeroth.runtime.runs:RunHistoryEntry` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:RunState` | `zeroth.runtime.runs:RunState` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:RunStatus` | `zeroth.runtime.runs:RunStatus` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:Thread` | `zeroth.runtime.runs:Thread` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:ThreadMemoryBinding` | `zeroth.runtime.runs:ThreadMemoryBinding` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:ThreadStatus` | `zeroth.runtime.runs:ThreadStatus` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |

`zeroth.core.runs:RunRepository` and `zeroth.core.runs:ThreadRepository` are
deliberately absent: the concrete repositories are persistence, not runtime
contracts, and they move to `zeroth.integrations.persistence.runs` in Task 6.
Runtime code depends on the new `RunReader`, `RunWriter`, `CheckpointStore`,
and `ThreadStore` protocols published alongside the models.

### Run serialization and checkpoint storage

The first half of that persistence move is done. Row-to-model conversion now
lives in `zeroth.integrations.persistence.runs.serialization` and the
`run_checkpoints` table adapter in
`zeroth.integrations.persistence.runs.checkpoint_store`.

Neither addition appears in the symbol migration log or the canonical surface,
and that is not an omission. Every symbol involved was a private helper —
`_row_to_run`, `_row_to_thread`, `_dump_model`, `_dump_list`,
`_new_checkpoint_id`, and the two `_*_state_json` methods — so none of them
carries a protected legacy capability ID. The log records public import
locations that consumers may depend on; it does not track internal structure.
`RunRepository` and `ThreadRepository` remain published from
`zeroth.core.runs`, and their entries move only when the repositories
themselves do.

The split follows the transaction boundary rather than the table names.
`checkpoint_store` owns the `run_checkpoints` rows and the at-rest encryption
of `state_json`; checkpoint *ordering* and the thread bookkeeping around a
write stay with the caller, because both read and write the thread record. In
the previous implementation each of those steps already opened its own
transaction, so delegating only the row write keeps the lock scope identical.
Moving the thread bookkeeping into the checkpoint adapter instead would have
merged transactions that were previously separate.

### Why these models are republished rather than relocated

The class definitions still live in `zeroth.core.runs.models`;
`zeroth.runtime.runs` re-exports the same class objects. This is a deliberate
constraint of the protected surface, not an oversight.

`inspect.signature` renders an annotation using the *defining* module of each
referenced type, so relocating these definitions rewrites signature strings
such as `list[zeroth.core.runs.models.RunHistoryEntry]`. Those exact strings are
pinned in the immutable `backend_surface_legacy.json`, and
`test_immutable_legacy_capabilities_remain_available_with_original_signatures`
compares the canonical entry against the legacy one. Relocation therefore fails
that gate whether or not the canonical fixture is updated, and the legacy
fixture may not be edited.

Physically relocating model definitions requires first making the surface
harness location-independent — normalizing `zeroth.*` module qualifiers out of
signature strings on both sides at comparison time, with the fixtures left
untouched. That is a deliberate amendment to a Task 1 contract and belongs in
its own commit, before the Task 12–16 package moves that cannot avoid it.

### Import-direction constraint while the models are republished

**No module reachable from `zeroth.core.__init__` may import
`zeroth.runtime.runs`.**

`zeroth.runtime.runs` re-exports models defined under `zeroth.core`, and
importing anything from `zeroth.core` executes its eager package `__init__`,
which pulls in most of the core graph. A module on that path that imports back
into `zeroth.runtime.runs` closes a cycle, and the canonical package stops
being importable in a cold interpreter:

```
ImportError: cannot import name 'Run' from partially initialized module
'zeroth.runtime.runs' (most likely due to a circular import)
```

The in-repo test suite cannot see this: `tests/conftest.py` imports
`zeroth.core.service.bootstrap` at collection time, so `zeroth.core` is always
warm before any test module loads. Only a library consumer — whose first
`zeroth` import may be `from zeroth.runtime.runs import Run` — hits it.
`tests/runtime/test_run_contracts.py::test_canonical_package_imports_in_a_cold_interpreter`
enforces the rule from a subprocess.

Consequently, run-model imports inside `zeroth.core` are **not** rewritten to
the canonical path yet. Each consumer is repointed when its own package leaves
`zeroth.core` — approvals in Task 13, the service APIs in Task 10, dispatch in
Task 11, and so on — because at that point it is no longer on the eager core
import path. Relocating the run models earlier would not lift this constraint
on its own: they also depend on `zeroth.core.governed` and
`zeroth.core.identity`, which trigger the same eager `__init__`.

## Updating the canonical surface

For a moved symbol, retain its immutable legacy capability ID in the canonical
entry's `legacy_ids`, change only the canonical `module` and `name`, add the
migration row above, and run both backend contract test modules. Multiple old
IDs may map to one canonical symbol only when the implementations are proven
semantically equivalent.
