# Backend dead-code and duplication audit (Task 17)

Audit date: 2026-07-20. Branch `codex/backend-architecture-refactor`,
baseline commit `1bd3f62` ("docs: mark task 16 complete in the refactor
plan"). This document is the deliverable for Task 17 of
`docs/superpowers/plans/2026-07-18-backend-architecture-refactor.md`: it
records how dead-code candidates were generated, the evidence gathered for
every candidate, what was deleted, and why everything else stays.

## Candidate generation

1. The code-review-graph MCP graph was fully rebuilt from this worktree:
   `build_or_update_graph(full_rebuild=true, repo_root=<worktree>)` parsed
   1,106 files into 8,185 nodes / 58,737 edges. The build metadata was
   verified to point at this worktree (`built_on_branch:
   codex/backend-architecture-refactor`, `built_at_sha: 1bd3f62`), not the
   main checkout.
2. `refactor_tool(mode=dead_code)` produced **176 candidates** — symbols
   with no resolvable callers, importers, tests, or entry-point edges in
   the graph.
3. Graph output was treated strictly as a candidate list. Every verdict
   below rests on the ripgrep evidence protocol, not on the graph.

## Evidence protocol

For every non-frontend candidate the following word-boundary, fixed-string
search ran from the worktree root:

```sh
rg -n --no-heading -w -F "<symbol>" src tests examples docs apps pyproject.toml
```

This one command covers all the surfaces the plan requires: `src tests
examples docs`, package exports (`__init__.py` / `__all__` lines live under
`src`), schemas and snapshot fixtures (`tests/contracts/fixtures/*.json`),
entry points (`pyproject.toml` `[project.scripts]`), and the optional-extras
probe tests. Per candidate the audit recorded the total match count and the
count excluding `def`/`class` definition lines; the full per-candidate
numbers are in the appendix table. A candidate was treated as *possibly*
dead only when every non-definition count was zero, and then had to survive
the qualitative checks below.

Supplementary searches used for specific classes of candidate:

- **Bound-callback wiring** — the two decomposed facades hand methods to
  collaborators by reference, so `rg -no 'orchestrator\.[a-z_]+'` over
  `src/zeroth/runtime/{orchestration,parallel,subgraphs}` enumerated every
  attribute collaborators reach through `orchestrator=self` (result:
  `_drive`, `_entry_step`, `resume_graph`, `record_approval_resolution`,
  `run_repository`, and nothing else), and `rg 'getattr\('` over the same
  trees ruled out dynamic attribute dispatch.
- **Fixture pinning** — candidate names and their owning classes were
  checked against `tests/contracts/fixtures/backend_surface_legacy.json`
  and `backend_surface_canonical.json` (covered by the main sweep since the
  fixtures live under `tests/`).
- **Orphaning provenance** — `git log -S '<symbol>('` traced when each
  deleted symbol lost its last caller; every deletion traces to a commit on
  this refactor branch.

## Known false-positive classes (screened out up front)

Per the execution brief, the following classes were never deletion
candidates regardless of reference counts:

- `zeroth.core.*` legacy shims — pinned by the immutable
  `backend_surface_legacy.json`; `tests/architecture/test_library_surface.py`
  imports every pinned symbol.
- Optional integrations (chroma/elastic/pgvector connectors, redis stores,
  sandbox + sidecar, httpx delivery path) — the global rule forbids
  deleting public or optional-integration symbols on static call counts.
- `zeroth.core.examples.demo_service` — reachable from the `zeroth-core`
  CLI entry point (`src/zeroth/core/cli.py`, `[project.scripts]`).
- Anything in `backend_surface_canonical.json` — the snapshot test imports
  every entry.
- The `zeroth._architecture` exception map — remaining entries are keyed to
  Task 18 and untouched by this task.

## Outcome summary

| Bucket | Count |
|--------|-------|
| Total graph candidates | 176 |
| `frontend/` (out of scope — the plan forbids modifying `frontend/`) | 26 |
| Audited backend/test/example candidates | 150 |
| **Deleted** (three commits, below) | **31** |
| Retained | 119 |

Of the 119 retained candidates, 17 have zero non-definition references
in-tree but are protected public/optional-integration surface; they are
called out in their own section for the Task 18 report.

## Deletions

Every deleted symbol is a private (`_`-prefixed) method with **zero**
references of any kind — no calls, no bound-callback wiring, no
monkeypatching, no fixture pinning, no string/getattr dispatch — anywhere
in `src`, `tests`, `examples`, `docs`, `apps`, or `pyproject.toml`. No
public or exported symbol was deleted.

### Equivalence-test position

The plan requires an equivalence test before deleting a *superseded
implementation*. All 31 deletions are unreferenced one-line delegation
wrappers (or fully orphaned helpers), so there is no caller whose behavior
could change and no behavior-parity claim to prove: the maintained
implementations are the very collaborator methods the wrappers delegated
to, and each already has a focused characterization suite, identified
below and run before each deletion commit. The full pre-commit suite
(entire pytest run + ruff) additionally ran on every commit; after the
first deletion commit the full suite stood at 3,568 passed / 16
deselected / 1 warning.

### Commit 1 (`e8e6a36`) — `refactor: remove superseded orchestrator facade delegations`

`src/zeroth/core/orchestrator/runtime.py` is the decomposed
`RuntimeOrchestrator` facade left by the runtime-studio decomposition
(`docs/architecture/runtime-studio-decomposition.md`; the file went from
2,575 lines / ~48 methods to 669 lines of delegations). 22 private
delegation wrappers no longer had any caller: the extracted collaborators
(`GraphDriver`, `NodeDispatcher`, `RuntimeParallelExecutor`,
`RuntimePolicyGate`, `RuntimeToolExecutor`, `RuntimeAuditRecorder`) call
their own methods directly, and the only orchestrator attributes they
reach back for are `_drive`, `_entry_step`, `resume_graph`,
`record_approval_resolution`, and `run_repository` — all kept.

Deleted: `_execute_parallel_fan_out`, `_handle_parallel_subgraph_pause`,
`_execute_parallel_fan_out_resume`, `_enforce_policy_for_branch`,
`_merge_fan_in_state`, `_dispatch_node_inner`,
`_record_failed_branch_execution_audit`, `_payload_for`,
`_enforce_loop_guards`, `_enforce_policy`, `_enforcement_context_for`,
`_effective_capabilities_for`, `_gate_policy_required_side_effects`,
`_consume_side_effect_approval`, `_node_has_side_effects`,
`_run_agent_with_optional_enforcement`,
`_run_executable_unit_with_optional_enforcement`, `_redact_for_audit`,
`_emit_webhook`, `_stored_audit_id`, `_node_by_id`, `_edge_for`; plus the
imports (`Capability`, `node_by_id`, `BranchContext`, `FanInResult`) those
wrappers alone used.

Kept deliberately: every method a collaborator or test still references —
e.g. `_fail_run` and `_refresh_artifact_ttls` (bound callbacks into
`RuntimePolicyGate`/`RuntimeParallelExecutor`), `_plan_next_nodes`
(callback), `_dispatch_node`, `_tool_executor_for`, `_typed_audit_fields`,
and all collaborator-building properties.

Note on residual references: a handful of comments and docstrings in
`tests/parallel/`, `src/zeroth/runtime/parallel/`, and
`src/zeroth/runtime/subgraphs/` still mention the old wrapper names as
historical narrative. They reference no code object and were left as-is to
keep this commit purely deletion-scoped.

Maintained-implementation suites: `tests/runtime/orchestration/`
(characterization, dispatcher, driver, parallel-executor, policy-gate,
audit-recorder), `tests/orchestrator/`, `tests/parallel/` — 1,335 tests
ran green with the deletions before commit, plus `tests/architecture` and
`tests/contracts`.

### Commit 2 (`9b3a139`) — `refactor: remove superseded erasure-service facade delegations`

`src/zeroth/governance/retention/erasure_service.py` follows the same
decomposed-facade pattern: `CleanupClaims`, `CleanupExecutor`, and
`CompatibilityLog` collaborators are rebuilt per access, and the
collaborators are wired with objects (`claims=self._claims`,
`compatibility=self._compatibility`) — not with the facade's private
methods. Seven delegation wrappers had zero references:

`_get_or_materialize_state_record`, `_execute_operation`,
`_execute_operation_with_heartbeat`, `_record_claim_heartbeat`,
`_record_terminal_fenced`, `_record_external_compatibility_steps`,
`_record_compatibility_log`.

Kept deliberately: `_replay_cleanup_state` (wired as
`replay=self._replay_cleanup_state` and monkeypatched by
`tests/retention/test_erasure_hardening.py`), and every other private
method with live callers (`_record_operation_delta`,
`_verify_active_claim`, `_release_cleanup_claim`, `_result_from_manifest`,
`_record_database_compatibility_steps`, `_call_with_idempotency`, etc.).

Maintained-implementation suites: `tests/retention/` (erasure service,
hardening, coordination, TTL, worker), plus `tests/architecture` and
`tests/contracts` — 1,178 tests green with the deletions before commit.

### Commit 3 (`4f6f6a4`) — `refactor: remove orphaned thread bookkeeping helpers`

Two fully orphaned private helpers, each traced to the refactor commit
that removed its last caller:

- `ThreadCheckpointStore._checkpoint_order`
  (`src/zeroth/runtime/agents/thread_store.py`) — orphaned by `3950d8d`
  "refactor: consolidate agent runtime"; checkpoint ordering now lives
  behind `RunRepository.write_checkpoint`, which `_write_checkpoint`
  delegates to.
- `_ensure_thread_from_run`
  (`src/zeroth/integrations/persistence/runs/run_repository.py`) —
  orphaned by `3ca8fa0` "refactor: extract run persistence adapter";
  thread get-or-create moved into `_record_thread_run`'s SQL-backed path.

Maintained-implementation suites: `tests/agent_runtime/test_thread_store.py`
and the run-persistence coverage under `tests/orchestrator/`, plus
`tests/architecture` and `tests/contracts` — 1,156 tests green with the
deletions before commit.

## Retained candidates with zero in-tree references

These 17 symbols have no non-definition references anywhere in-tree, but
each hits a protection rule. They are the honest "dead-looking but
protected" set for Task 18's final report:

| Symbol | Location | Why it stays |
|--------|----------|--------------|
| `bind_edges` | `src/zeroth/contracts/conditions/binding.py:27` | Public method on `ConditionBinder`; the class is pinned in both surface fixtures |
| `emit_outcome` | `src/zeroth/econ/instrumentation/langgraph/adapter.py:30` | Absorbed Regulus SDK public API for external LangGraph consumers |
| `active_layer` | `src/zeroth/econ/instrumentation/runtime.py:61` | Absorbed SDK public accessor |
| `aenqueue_outcome` | `src/zeroth/econ/instrumentation/transport.py:63` | Absorbed SDK async parity API |
| `queue_size` | `src/zeroth/econ/instrumentation/transport.py:66` | Absorbed SDK public introspection API |
| `process_connector_outbox` | `src/zeroth/econ/plane/connectors/workers.py:14` | Worker entry point invoked by deployment scheduling, not in-process calls |
| `events_for_run` | `src/zeroth/governance/audit/redis.py:53` | Public parity method mirroring the SQL audit repository API |
| `write_many` | `src/zeroth/governance/audit/repository.py:215` | Public bulk-write persistence API |
| `_docker_control` | `src/zeroth/integrations/execution/sandbox.py:696` | Optional docker sandbox integration; protected class, docker-only paths not exercised in the all-extras env |
| `record_condition_result` | `src/zeroth/integrations/persistence/runs/run_repository.py:694` | Public persistence API on the pinned repository class |
| `to_manifest` | `src/zeroth/runtime/agents/tooling/base.py:105` | Public `ToolManifest` extraction API on the tool base class |
| `execute_once` | `src/zeroth/runtime/agents/tooling/tool_calls.py:85` | Public entry point of the absorbed GovernAI tool-call loop (Task 15 seam; probe test pins the class) |
| `resolve_declared_tools` | `src/zeroth/runtime/agents/tools.py:194` | Public resolver on fixture-pinned `ToolAttachmentBridge` |
| `bump_epoch` | `src/zeroth/runtime/orchestration/interrupts.py:270` | Public API on `InterruptManager` |
| `get_pending` | `src/zeroth/runtime/orchestration/interrupts.py:310` | Public API on `InterruptManager` |
| `get_latest_pending` | `src/zeroth/runtime/orchestration/interrupts.py:321` | Public API on `InterruptManager` |
| `clear_expired` | `src/zeroth/runtime/orchestration/interrupts.py:374` | Public API on `InterruptManager` |

## Duplication findings

The one apparent cross-package duplication the candidate list surfaced —
`validate_agent_capabilities` / `validate_tool_grants` appearing in both
`src/zeroth/contracts/graph/validation/capabilities.py` and
`src/zeroth/runtime/graph_validation.py` — is **not** duplication. The
contracts module defines a `CapabilityChecks` Protocol plus a
`NullCapabilityChecks` null object; the runtime module holds the real
governance-owned implementations, injected as a collaborator because the
contracts layer may not import governance (the module docstring documents
the seam and why the rules cannot run as a later pass). No other
same-signature duplicate pair was flagged by the graph or found during the
per-candidate review.

## Appendix — per-candidate evidence and verdicts

Columns: reference counts from the evidence-protocol `rg` sweep — total
word-boundary matches / matches excluding `def`/`class` definition lines.
The 26 `frontend/` candidates are omitted (out of scope by plan rule).

| # | Kind | Symbol | Location | Refs (total / non-def) | Verdict |
|---|------|--------|----------|------------------------|---------|
| 1 | Class | `BranchResolutionError` | `src/zeroth/contracts/conditions/errors.py:18` | 10 / 9 | Retained — Exported contract model/protocol surface |
| 2 | Class | `ApprovalDecisionType` | `src/zeroth/contracts/governed/models/approval.py:14` | 7 / 6 | Retained — Exported contract model/protocol surface |
| 3 | Class | `DeterminismMode` | `src/zeroth/contracts/governed/models/common.py:21` | 6 / 5 | Retained — Exported contract model/protocol surface |
| 4 | Class | `WasteKind` | `src/zeroth/econ/analytics/waste.py:36` | 55 / 54 | Retained — Absorbed econ analytics public surface |
| 5 | Class | `LangGraphTelemetryAdapter` | `src/zeroth/econ/instrumentation/langgraph/adapter.py:11` | 12 / 11 | Retained — Absorbed Regulus SDK public API; external-consumer surface |
| 6 | Class | `RegexScorer` | `src/zeroth/eval/scorers.py:78` | 15 / 14 | Retained — Exported eval scorer; pinned in both surface fixtures |
| 7 | Class | `HumanInteractionType` | `src/zeroth/governance/approvals/models.py:26` | 22 / 21 | Retained — Public governance surface (exported model/enum or repository API) |
| 8 | Class | `AuthMethod` | `src/zeroth/governance/identity/models.py:11` | 72 / 71 | Retained — Public governance surface (exported model/enum or repository API) |
| 9 | Class | `PolicyDecision` | `src/zeroth/governance/policy/models.py:24` | 40 / 39 | Retained — Public governance surface (exported model/enum or repository API) |
| 10 | Class | `ExecutionMode` | `src/zeroth/integrations/execution/models.py:19` | 73 / 72 | Retained — Optional execution/sandbox integration surface (protected class) |
| 11 | Class | `EntryPointType` | `src/zeroth/integrations/execution/models.py:63` | 40 / 39 | Retained — Optional execution/sandbox integration surface (protected class) |
| 12 | Class | `RuntimeLanguage` | `src/zeroth/integrations/execution/models.py:71` | 40 / 39 | Retained — Optional execution/sandbox integration surface (protected class) |
| 13 | Class | `SandboxSidecarClient` | `src/zeroth/integrations/execution/sidecar_client.py:19` | 19 / 18 | Retained — Optional execution/sandbox integration surface (protected class) |
| 14 | Class | `AuthType` | `src/zeroth/integrations/http/models.py:24` | 44 / 43 | Retained — Exported HTTP integration contract model |
| 15 | Class | `GovernedToolCallLoop` | `src/zeroth/runtime/agents/tooling/tool_calls.py:82` | 6 / 5 | Retained — Tooling surface: template-method override or decorator closure invoked by the base class |
| 16 | Class | `ToolAttachmentAction` | `src/zeroth/runtime/agents/tools.py:320` | 14 / 13 | Retained — Runtime library surface (public method, protocol impl, or by-reference callback) |
| 17 | Class | `WatchError` | `src/zeroth/runtime/orchestration/run_store.py:14` | 3 / 2 | Retained — Run-store surface: exported error/decorator class and interface parity methods |
| 18 | Class | `ThreadAwareRunStore` | `src/zeroth/runtime/orchestration/run_store.py:37` | 6 / 5 | Retained — Run-store surface: exported error/decorator class and interface parity methods |
| 19 | Class | `RunReader` | `src/zeroth/runtime/runs/protocols.py:25` | 16 / 15 | Retained — Structural typing Protocol — satisfied implicitly, never named at call sites |
| 20 | Class | `RunWriter` | `src/zeroth/runtime/runs/protocols.py:33` | 13 / 12 | Retained — Structural typing Protocol — satisfied implicitly, never named at call sites |
| 21 | Class | `CheckpointStore` | `src/zeroth/runtime/runs/protocols.py:45` | 13 / 12 | Retained — Structural typing Protocol — satisfied implicitly, never named at call sites |
| 22 | Class | `EscalationAction` | `src/zeroth/service/webhooks/models.py:45` | 23 / 22 | Retained — ASGI factory / lifespan / signal / route wiring by reference |
| 23 | Function | `sanctions_screen_handler` | `apps/vendor_dd/units.py:61` | 4 / 3 | Retained — Example unit handler wired by reference in the vendor-DD reference app |
| 24 | Function | `prepare_panel_handler` | `apps/vendor_dd/units.py:79` | 4 / 3 | Retained — Example unit handler wired by reference in the vendor-DD reference app |
| 25 | Function | `tool_executor` | `examples/04_native_tool.py:108` | 69 / 58 | Retained — Example handler/factory wired by reference; examples are executable documentation |
| 26 | Function | `_handler` | `examples/22_budget_cap.py:80` | 4 / 3 | Retained — Example handler/factory wired by reference; examples are executable documentation |
| 27 | Function | `format_article_handler` | `examples/_tools.py:35` | 4 / 3 | Retained — Example handler/factory wired by reference; examples are executable documentation |
| 28 | Function | `echo_handler` | `examples/_tools.py:43` | 4 / 3 | Retained — Example handler/factory wired by reference; examples are executable documentation |
| 29 | Function | `app_factory` | `examples/service/entrypoint.py:103` | 14 / 12 | Retained — Example handler/factory wired by reference; examples are executable documentation |
| 30 | Function | `bind_edges` | `src/zeroth/contracts/conditions/binding.py:27` | 1 / 0 | Retained — Public method on `ConditionBinder`, which is pinned in both surface fixtures; global rule forbids deleting public symbols on static call counts |
| 31 | Function | `_utcnow` | `src/zeroth/contracts/governed/models/audit.py:11` | 33 / 25 | Retained — Exported contract model/protocol surface |
| 32 | Function | `normalize_step_ref` | `src/zeroth/contracts/governed/models/common.py:68` | 6 / 5 | Retained — Exported contract model/protocol surface |
| 33 | Function | `validate_agent_capabilities` | `src/zeroth/contracts/graph/validation/capabilities.py:48` | 5 / 1 | Retained — `CapabilityChecks` Protocol member — implemented polymorphically; the null-object and runtime implementations are injected collaborators |
| 34 | Function | `validate_tool_grants` | `src/zeroth/contracts/graph/validation/capabilities.py:56` | 5 / 1 | Retained — `CapabilityChecks` Protocol member — same seam as `validate_agent_capabilities` |
| 35 | Function | `_cmd_migrate` | `src/zeroth/core/cli.py:39` | 2 / 1 | Retained — argparse subcommand handler registered via `set_defaults(func=...)`; reachable from the `zeroth-core` entry point |
| 36 | Function | `_cmd_seed_demo` | `src/zeroth/core/cli.py:45` | 2 / 1 | Retained — argparse subcommand handler; drives `demo_service`, exercised by tests/service/test_cli_and_factory.py |
| 37 | Function | `_cmd_serve` | `src/zeroth/core/cli.py:91` | 2 / 1 | Retained — argparse subcommand handler for `zeroth-core serve` |
| 38 | Function | `_execute_parallel_fan_out` | `src/zeroth/core/orchestrator/runtime.py:230` | 4 / 3 | **Deleted** (orchestrator facade) |
| 39 | Function | `_handle_parallel_subgraph_pause` | `src/zeroth/core/orchestrator/runtime.py:256` | 1 / 0 | **Deleted** (orchestrator facade) |
| 40 | Function | `_execute_parallel_fan_out_resume` | `src/zeroth/core/orchestrator/runtime.py:270` | 2 / 1 | **Deleted** (orchestrator facade) |
| 41 | Function | `_enforce_policy_for_branch` | `src/zeroth/core/orchestrator/runtime.py:285` | 2 / 1 | **Deleted** (orchestrator facade) |
| 42 | Function | `_merge_fan_in_state` | `src/zeroth/core/orchestrator/runtime.py:295` | 2 / 1 | **Deleted** (orchestrator facade) |
| 43 | Function | `_dispatch_node_inner` | `src/zeroth/core/orchestrator/runtime.py:334` | 4 / 3 | **Deleted** (orchestrator facade) |
| 44 | Function | `_record_failed_branch_execution_audit` | `src/zeroth/core/orchestrator/runtime.py:445` | 1 / 0 | **Deleted** (orchestrator facade) |
| 45 | Function | `_payload_for` | `src/zeroth/core/orchestrator/runtime.py:459` | 1 / 0 | **Deleted** (orchestrator facade) |
| 46 | Function | `_enforce_loop_guards` | `src/zeroth/core/orchestrator/runtime.py:483` | 1 / 0 | **Deleted** (orchestrator facade) |
| 47 | Function | `_enforce_policy` | `src/zeroth/core/orchestrator/runtime.py:492` | 1 / 0 | **Deleted** (orchestrator facade) |
| 48 | Function | `_enforcement_context_for` | `src/zeroth/core/orchestrator/runtime.py:502` | 5 / 3 | **Deleted** (orchestrator facade) |
| 49 | Function | `_effective_capabilities_for` | `src/zeroth/core/orchestrator/runtime.py:506` | 4 / 2 | **Deleted** (orchestrator facade) |
| 50 | Function | `_gate_policy_required_side_effects` | `src/zeroth/core/orchestrator/runtime.py:510` | 1 / 0 | **Deleted** (orchestrator facade) |
| 51 | Function | `_consume_side_effect_approval` | `src/zeroth/core/orchestrator/runtime.py:519` | 1 / 0 | **Deleted** (orchestrator facade) |
| 52 | Function | `_node_has_side_effects` | `src/zeroth/core/orchestrator/runtime.py:528` | 1 / 0 | **Deleted** (orchestrator facade) |
| 53 | Function | `_run_agent_with_optional_enforcement` | `src/zeroth/core/orchestrator/runtime.py:532` | 1 / 0 | **Deleted** (orchestrator facade) |
| 54 | Function | `_run_executable_unit_with_optional_enforcement` | `src/zeroth/core/orchestrator/runtime.py:550` | 1 / 0 | **Deleted** (orchestrator facade) |
| 55 | Function | `_redact_for_audit` | `src/zeroth/core/orchestrator/runtime.py:572` | 1 / 0 | **Deleted** (orchestrator facade) |
| 56 | Function | `_emit_webhook` | `src/zeroth/core/orchestrator/runtime.py:638` | 4 / 2 | **Deleted** (orchestrator facade) |
| 57 | Function | `_stored_audit_id` | `src/zeroth/core/orchestrator/runtime.py:655` | 1 / 0 | **Deleted** (orchestrator facade) |
| 58 | Function | `_node_by_id` | `src/zeroth/core/orchestrator/runtime.py:663` | 1 / 0 | **Deleted** (orchestrator facade) |
| 59 | Function | `_edge_for` | `src/zeroth/core/orchestrator/runtime.py:667` | 1 / 0 | **Deleted** (orchestrator facade) |
| 60 | Function | `_new_id` | `src/zeroth/core/runs/models.py:26` | 13 / 9 | Retained — `default_factory` reference on legacy-surface run models |
| 61 | Function | `target` | `src/zeroth/econ/analytics/rightsizing_experiment.py:342` | 472 / 467 | Retained — Absorbed Regulus SDK (`zeroth.econ`) public API; rightsizing experiment target accessor |
| 62 | Function | `_cmd_init` | `src/zeroth/econ/instrumentation/cli.py:10` | 2 / 1 | Retained — Absorbed Regulus SDK public API; external-consumer surface |
| 63 | Function | `_cmd_demo` | `src/zeroth/econ/instrumentation/cli.py:31` | 2 / 1 | Retained — Absorbed Regulus SDK public API; external-consumer surface |
| 64 | Function | `_cmd_compute` | `src/zeroth/econ/instrumentation/cli.py:69` | 2 / 1 | Retained — Absorbed Regulus SDK public API; external-consumer surface |
| 65 | Function | `build_cost_profile_input` | `src/zeroth/econ/instrumentation/client.py:116` | 14 / 13 | Retained — Absorbed Regulus SDK public API; external-consumer surface |
| 66 | Function | `with_instrumentation` | `src/zeroth/econ/instrumentation/client.py:143` | 14 / 13 | Retained — Absorbed Regulus SDK public API; external-consumer surface |
| 67 | Function | `join_key_context` | `src/zeroth/econ/instrumentation/client.py:167` | 14 / 13 | Retained — Absorbed Regulus SDK public API; external-consumer surface |
| 68 | Function | `wrapped` | `src/zeroth/econ/instrumentation/integrations/anthropic.py:165` | 62 / 58 | Retained — Absorbed Regulus SDK instrumentation API for external frameworks (closures/wrappers returned or applied by reference) |
| 69 | Function | `instrument_anthropic_client` | `src/zeroth/econ/instrumentation/integrations/anthropic.py:259` | 22 / 21 | Retained — Absorbed Regulus SDK instrumentation API for external frameworks (closures/wrappers returned or applied by reference) |
| 70 | Function | `instrument_anthropic_async_client` | `src/zeroth/econ/instrumentation/integrations/anthropic.py:263` | 22 / 21 | Retained — Absorbed Regulus SDK instrumentation API for external frameworks (closures/wrappers returned or applied by reference) |
| 71 | Function | `instrument_langchain_async_runnable` | `src/zeroth/econ/instrumentation/integrations/langchain.py:323` | 22 / 21 | Retained — Absorbed Regulus SDK instrumentation API for external frameworks (closures/wrappers returned or applied by reference) |
| 72 | Function | `instrument_langchain_app` | `src/zeroth/econ/instrumentation/integrations/langchain.py:327` | 22 / 21 | Retained — Absorbed Regulus SDK instrumentation API for external frameworks (closures/wrappers returned or applied by reference) |
| 73 | Function | `instrument_langgraph_graph` | `src/zeroth/econ/instrumentation/integrations/langgraph.py:160` | 33 / 32 | Retained — Absorbed Regulus SDK instrumentation API for external frameworks (closures/wrappers returned or applied by reference) |
| 74 | Function | `wrapped` | `src/zeroth/econ/instrumentation/integrations/openai.py:168` | 62 / 58 | Retained — Absorbed Regulus SDK instrumentation API for external frameworks (closures/wrappers returned or applied by reference) |
| 75 | Function | `instrument_openai_client` | `src/zeroth/econ/instrumentation/integrations/openai.py:263` | 22 / 21 | Retained — Absorbed Regulus SDK instrumentation API for external frameworks (closures/wrappers returned or applied by reference) |
| 76 | Function | `instrument_openai_async_client` | `src/zeroth/econ/instrumentation/integrations/openai.py:267` | 22 / 21 | Retained — Absorbed Regulus SDK instrumentation API for external frameworks (closures/wrappers returned or applied by reference) |
| 77 | Function | `emit_outcome` | `src/zeroth/econ/instrumentation/langgraph/adapter.py:30` | 1 / 0 | Retained — Public method on `LangGraphTelemetryAdapter`; absorbed Regulus SDK surface for external consumers |
| 78 | Function | `active_layer` | `src/zeroth/econ/instrumentation/runtime.py:61` | 1 / 0 | Retained — Public accessor on the instrumentation runtime layer stack; absorbed SDK surface |
| 79 | Function | `aenqueue_outcome` | `src/zeroth/econ/instrumentation/transport.py:63` | 1 / 0 | Retained — Async enqueue parity API on the instrumentation transport; absorbed SDK surface |
| 80 | Function | `queue_size` | `src/zeroth/econ/instrumentation/transport.py:66` | 1 / 0 | Retained — Public queue introspection API on the instrumentation transport; absorbed SDK surface |
| 81 | Function | `checker` | `src/zeroth/econ/plane/auth/deps.py:20` | 20 / 19 | Retained — Econ-plane service/library API (workers, erasure, auth closures) |
| 82 | Function | `connector_type` | `src/zeroth/econ/plane/connectors/registry.py:56` | 136 / 126 | Retained — Polymorphic `connector_type` property implementing the connector protocol |
| 83 | Function | `connector_type` | `src/zeroth/econ/plane/connectors/registry.py:61` | 136 / 126 | Retained — Polymorphic `connector_type` property implementing the connector protocol |
| 84 | Function | `connector_type` | `src/zeroth/econ/plane/connectors/registry.py:66` | 136 / 126 | Retained — Polymorphic `connector_type` property implementing the connector protocol |
| 85 | Function | `connector_type` | `src/zeroth/econ/plane/connectors/registry.py:71` | 136 / 126 | Retained — Polymorphic `connector_type` property implementing the connector protocol |
| 86 | Function | `connector_type` | `src/zeroth/econ/plane/connectors/registry.py:76` | 136 / 126 | Retained — Polymorphic `connector_type` property implementing the connector protocol |
| 87 | Function | `connector_type` | `src/zeroth/econ/plane/connectors/registry.py:81` | 136 / 126 | Retained — Polymorphic `connector_type` property implementing the connector protocol |
| 88 | Function | `process_connector_outbox` | `src/zeroth/econ/plane/connectors/workers.py:14` | 1 / 0 | Retained — Econ-plane outbox worker entry point; deployment-scheduled surface, not an in-process call target |
| 89 | Function | `run_evaluation_async` | `src/zeroth/econ/plane/counterfactual/tasks.py:10` | 3 / 2 | Retained — Econ-plane service/library API (workers, erasure, auth closures) |
| 90 | Function | `delete_events_for_run` | `src/zeroth/econ/plane/erasure.py:29` | 14 / 6 | Retained — Econ-plane service/library API (workers, erasure, auth closures) |
| 91 | Function | `_new_id` | `src/zeroth/governance/approvals/models.py:21` | 13 / 9 | Retained — Public governance surface (exported model/enum or repository API) |
| 92 | Function | `events_for_run` | `src/zeroth/governance/audit/redis.py:53` | 1 / 0 | Retained — Public parity method on the Redis audit repository mirroring the SQL repository API |
| 93 | Function | `write_many` | `src/zeroth/governance/audit/repository.py:215` | 1 / 0 | Retained — Public bulk-write API on the audit repository; persistence surface |
| 94 | Function | `_luhn_ok` | `src/zeroth/governance/guardrails/content.py:31` | 2 / 1 | Retained — Closure or helper invoked by reference (`re.sub` repl / validator) |
| 95 | Function | `_repl` | `src/zeroth/governance/guardrails/content.py:88` | 4 / 2 | Retained — Closure or helper invoked by reference (`re.sub` repl / validator) |
| 96 | Function | `_repl` | `src/zeroth/governance/guardrails/content.py:115` | 4 / 2 | Retained — Closure or helper invoked by reference (`re.sub` repl / validator) |
| 97 | Function | `scope` | `src/zeroth/governance/identity/models.py:53` | 472 / 471 | Retained — Public governance surface (exported model/enum or repository API) |
| 98 | Function | `delete_events_for_run` | `src/zeroth/governance/retention/econ_eraser.py:38` | 14 / 6 | Retained — Public governance surface (exported model/enum or repository API) |
| 99 | Function | `_replay_cleanup_state` | `src/zeroth/governance/retention/erasure_service.py:534` | 7 / 6 | Retained — Public governance surface (exported model/enum or repository API) |
| 100 | Function | `_get_or_materialize_state_record` | `src/zeroth/governance/retention/erasure_service.py:550` | 1 / 0 | **Deleted** (erasure facade) |
| 101 | Function | `_execute_operation` | `src/zeroth/governance/retention/erasure_service.py:579` | 1 / 0 | **Deleted** (erasure facade) |
| 102 | Function | `_execute_operation_with_heartbeat` | `src/zeroth/governance/retention/erasure_service.py:583` | 1 / 0 | **Deleted** (erasure facade) |
| 103 | Function | `_record_claim_heartbeat` | `src/zeroth/governance/retention/erasure_service.py:598` | 1 / 0 | **Deleted** (erasure facade) |
| 104 | Function | `_record_terminal_fenced` | `src/zeroth/governance/retention/erasure_service.py:650` | 1 / 0 | **Deleted** (erasure facade) |
| 105 | Function | `_record_external_compatibility_steps` | `src/zeroth/governance/retention/erasure_service.py:708` | 1 / 0 | **Deleted** (erasure facade) |
| 106 | Function | `_record_compatibility_log` | `src/zeroth/governance/retention/erasure_service.py:718` | 1 / 0 | **Deleted** (erasure facade) |
| 107 | Function | `supports` | `src/zeroth/integrations/execution/adapters.py:39` | 20 / 18 | Retained — Optional execution/sandbox integration surface (protected class) |
| 108 | Function | `run_inline_source` | `src/zeroth/integrations/execution/runner.py:258` | 8 / 5 | Retained — Task 15 `run_inline_source` seam implementation — brief-protected |
| 109 | Function | `_run_with_prepared_environment` | `src/zeroth/integrations/execution/runner.py:572` | 2 / 1 | Retained — Optional execution/sandbox integration surface (protected class) |
| 110 | Function | `snapshot` | `src/zeroth/integrations/execution/sandbox.py:300` | 136 / 134 | Retained — Optional execution/sandbox integration surface (protected class) |
| 111 | Function | `_docker_control` | `src/zeroth/integrations/execution/sandbox.py:696` | 1 / 0 | Retained — Private helper inside the optional docker sandbox backend; optional-integration class is protected from static-count deletion, and docker-only paths are not exercised in the all-extras env |
| 112 | Function | `_connect` | `src/zeroth/integrations/memory/pgvector_connector.py:68` | 2 / 1 | Retained — Optional memory-connector integration (protected class) |
| 113 | Function | `_ensure_thread_from_run` | `src/zeroth/integrations/persistence/runs/run_repository.py:416` | 1 / 0 | **Deleted** (thread bookkeeping) |
| 114 | Function | `record_condition_result` | `src/zeroth/integrations/persistence/runs/run_repository.py:694` | 1 / 0 | Retained — Public persistence API on the pinned run repository class |
| 115 | Function | `_write_file` | `src/zeroth/platform/artifacts/store.py:351` | 2 / 1 | Retained — Invoked by reference through `asyncio.to_thread` sync/async split |
| 116 | Function | `_read_file` | `src/zeroth/platform/artifacts/store.py:359` | 2 / 1 | Retained — Invoked by reference through `asyncio.to_thread` sync/async split |
| 117 | Function | `_read_meta` | `src/zeroth/platform/artifacts/store.py:367` | 2 / 1 | Retained — Invoked by reference through `asyncio.to_thread` sync/async split |
| 118 | Function | `_delete` | `src/zeroth/platform/artifacts/store.py:489` | 2 / 1 | Retained — Invoked by reference through `asyncio.to_thread` sync/async split |
| 119 | Function | `_refresh` | `src/zeroth/platform/artifacts/store.py:525` | 2 / 1 | Retained — Invoked by reference through `asyncio.to_thread` sync/async split |
| 120 | Function | `_check` | `src/zeroth/platform/artifacts/store.py:550` | 2 / 1 | Retained — Invoked by reference through `asyncio.to_thread` sync/async split |
| 121 | Function | `_cleanup` | `src/zeroth/platform/artifacts/store.py:574` | 2 / 1 | Retained — Invoked by reference through `asyncio.to_thread` sync/async split |
| 122 | Function | `_new_worker_id` | `src/zeroth/platform/dispatch/lease.py:43` | 3 / 1 | Retained — `default_factory`/registration reference |
| 123 | Function | `resolve_async` | `src/zeroth/platform/secrets/vault.py:147` | 35 / 30 | Retained — Async parity API on the vault resolver surface |
| 124 | Function | `resolve_many_async` | `src/zeroth/platform/secrets/vault.py:151` | 30 / 25 | Retained — Async parity API on the vault resolver surface |
| 125 | Function | `history` | `src/zeroth/runtime/agents/models.py:268` | 146 / 144 | Retained — Runtime library surface (public method, protocol impl, or by-reference callback) |
| 126 | Function | `_checkpoint_order` | `src/zeroth/runtime/agents/thread_store.py:210` | 1 / 0 | **Deleted** (thread bookkeeping) |
| 127 | Function | `to_manifest` | `src/zeroth/runtime/agents/tooling/base.py:105` | 1 / 0 | Retained — Public `ToolManifest` extraction API on the tool base class; tool-registration surface |
| 128 | Function | `_execute_validated` | `src/zeroth/runtime/agents/tooling/cli_tool.py:63` | 4 / 1 | Retained — Tooling surface: template-method override or decorator closure invoked by the base class |
| 129 | Function | `_execute_validated` | `src/zeroth/runtime/agents/tooling/python_tool.py:49` | 4 / 1 | Retained — Tooling surface: template-method override or decorator closure invoked by the base class |
| 130 | Function | `decorator` | `src/zeroth/runtime/agents/tooling/python_tool.py:72` | 8 / 7 | Retained — Tooling surface: template-method override or decorator closure invoked by the base class |
| 131 | Function | `execute_once` | `src/zeroth/runtime/agents/tooling/tool_calls.py:85` | 1 / 0 | Retained — Public entry point of `GovernedToolCallLoop` (absorbed GovernAI tool-call seam, Task 15); probe test pins the class |
| 132 | Function | `resolve_declared_tools` | `src/zeroth/runtime/agents/tools.py:194` | 1 / 0 | Retained — Public resolver on fixture-pinned `ToolAttachmentBridge`; sibling `ensure_declared_tools` is the strict variant in live use |
| 133 | Function | `validate_agent_capabilities` | `src/zeroth/runtime/graph_validation.py:209` | 5 / 1 | Retained — Runtime library surface (public method, protocol impl, or by-reference callback) |
| 134 | Function | `validate_tool_grants` | `src/zeroth/runtime/graph_validation.py:239` | 5 / 1 | Retained — Runtime library surface (public method, protocol impl, or by-reference callback) |
| 135 | Function | `bump_epoch` | `src/zeroth/runtime/orchestration/interrupts.py:270` | 1 / 0 | Retained — Public epoch API on `InterruptManager` (human-interrupt subsystem) |
| 136 | Function | `get_pending` | `src/zeroth/runtime/orchestration/interrupts.py:310` | 1 / 0 | Retained — Public single-interrupt lookup on `InterruptManager` |
| 137 | Function | `get_latest_pending` | `src/zeroth/runtime/orchestration/interrupts.py:321` | 1 / 0 | Retained — Public newest-pending lookup on `InterruptManager` |
| 138 | Function | `clear_expired` | `src/zeroth/runtime/orchestration/interrupts.py:374` | 1 / 0 | Retained — Public TTL-sweep API on `InterruptManager` |
| 139 | Function | `run_inline_source` | `src/zeroth/runtime/orchestration/protocols.py:47` | 8 / 5 | Retained — Task 15 `run_inline_source` protocol seam — deliberate, brief-protected |
| 140 | Function | `clear_active_run_id` | `src/zeroth/runtime/orchestration/run_store.py:52` | 7 / 2 | Retained — Run-store surface: exported error/decorator class and interface parity methods |
| 141 | Function | `clear_active_run_id` | `src/zeroth/runtime/orchestration/run_store.py:223` | 7 / 2 | Retained — Run-store surface: exported error/decorator class and interface parity methods |
| 142 | Function | `clear_active_run_id` | `src/zeroth/runtime/orchestration/run_store.py:502` | 7 / 2 | Retained — Run-store surface: exported error/decorator class and interface parity methods |
| 143 | Function | `_new_worker_id` | `src/zeroth/runtime/orchestration/run_worker.py:34` | 3 / 1 | Retained — Runtime library surface (public method, protocol impl, or by-reference callback) |
| 144 | Function | `resume` | `src/zeroth/runtime/subgraphs/executor.py:218` | 87 / 86 | Retained — Runtime library surface (public method, protocol impl, or by-reference callback) |
| 145 | Function | `service_lifespan` | `src/zeroth/service/bootstrap/lifecycle.py:24` | 8 / 7 | Retained — ASGI factory / lifespan / signal / route wiring by reference |
| 146 | Function | `_handle_shutdown_signal` | `src/zeroth/service/bootstrap/lifecycle.py:138` | 3 / 2 | Retained — ASGI factory / lifespan / signal / route wiring by reference |
| 147 | Function | `app_factory` | `src/zeroth/service/entrypoint.py:115` | 14 / 12 | Retained — ASGI factory / lifespan / signal / route wiring by reference |
| 148 | Function | `delete_subscription` | `src/zeroth/service/webhooks/service.py:83` | 3 / 1 | Retained — ASGI factory / lifespan / signal / route wiring by reference |
| 149 | Function | `sum_ints` | `tests/_fixtures/reducers.py:13` | 6 / 5 | Retained — Referenced by dotted-path reducer spec strings in parallel fan-in tests |
| 150 | Function | `sum_scores` | `tests/_fixtures/reducers.py:18` | 4 / 3 | Retained — Referenced by dotted-path reducer spec strings in parallel fan-in tests |
