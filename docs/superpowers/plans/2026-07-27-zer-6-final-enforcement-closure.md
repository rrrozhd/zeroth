# ZER-6 Final Enforcement Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four cycle-6 high findings by making the callable surface source-free, guarding shared state cells, and deleting all inner LangChain execution after authorization.

**Architecture:** Snapshot callables into guarded frozen bodies, store plain-callable plans behind opaque registry tokens, and execute `BaseTool` snapshots directly from the already-validated outer wrapper. The only post-decision operations are the state-cell escalation check and the frozen body call.

**Tech Stack:** Python 3.12, LangChain Core 1.x, Pydantic 2, pytest, Ruff, uv.

**Design:** `docs/superpowers/specs/2026-07-27-zer-6-final-enforcement-closure-design.md`

---

## File map

- Modify `src/zeroth/integrations/langgraph/_tool_execution.py`: guarded callable snapshots, state-cell checks, and direct sync/async snapshot execution; remove the inner `StructuredTool` executor.
- Modify `src/zeroth/integrations/langgraph/_tool_wrappers.py`: opaque-token callable registry, attested annotation copying, direct execution wiring, and outer error/output field ownership.
- Modify `tests/integrations/langgraph/tools/test_tool_substitution.py`: security regressions for publication, state-cell escalation, ambient config, global hooks, and nested callback behavior.
- Modify `tests/integrations/langgraph/tools/test_tool_wrappers.py`: response format and handled-error compatibility.
- Modify `docs/how-to/cookbook/govern-langgraph-tools.md`: one outer callback tree and direct frozen-body semantics.
- Modify `CHANGELOG.md`, `pyproject.toml`, `uv.lock`, `frontend/app/lib/version.ts`, and `tests/docs/test_release_metadata.py`: release `0.13.14.8`.

### Task 1: Pin source publication failures

**Files:**
- Modify: `tests/integrations/langgraph/tools/test_tool_substitution.py`

- [ ] **Step 1: Write failing recursive-introspection tests**

Add sync and async tests that create a callable whose annotation graph includes direct,
nested-container, `typing.Annotated`, and custom attribute-bearing references. Assert unsafe
annotations fail at wrap time with `ToolGovernanceError`. For an ordinary annotated callable,
recursively inspect `__dict__`, `__annotations__`, `__signature__`, `__defaults__`,
`__kwdefaults__`, and `__closure__`; assert neither the original, a frozen executable, nor a
plan containing one is reachable, and verify every extracted callable is metadata/framework
code rather than an unguarded tool body.

Add the same fail-closed checks for signature defaults: callable/custom defaults that cannot be
attested must not be published through `__signature__`. Add an explicit `args_schema` carrier
whose own static class/object dictionary retains the source and assert wrapping is refused;
retain a positive test proving an ordinary Pydantic schema object is published by identity.
Add registry lifecycle tests proving two wrappers have distinct tokens/plans, collecting one
wrapper removes exactly its entry, the live wrapper remains callable, and finalizers neither
retain wrappers nor leak entries.

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/integrations/langgraph/tools/test_tool_substitution.py -k 'publication or annotation or signature_default or args_schema_carrier or registry_lifecycle'`

Expected: FAIL because `functools.wraps` copies annotations and closure cells expose `target`
and `plan.target`.

- [ ] **Step 3: Implement the source-free callable registry**

In `_tool_wrappers.py`:

- add a module-private `dict[object, _CallablePlan]` registry;
- register a frozen callable plan under a fresh `object()` token and remove it with
  `weakref.finalize` when the governed wrapper is collected;
- introduce a callable-specific plan containing immutable name/description/schema facts, a
  frozen executable source, binding, and seams; it contains no original callable and does not
  reuse `_GovernedPlan.target`;
- make sync/async wrapper factories close over only the token and dispatch through a module
  helper that resolves the plan;
- build wrappers without `functools.wraps` and copy only name/doc/module/qualname strings plus
  recursively attested annotations;
- admit only builtin annotation atoms and recursively attested builtin/typing compositions;
  reject opaque/custom annotation classes or objects;
- attest signature defaults with the same fail-closed rule so `__signature__` cannot publish an
  executable/custom carrier;
- attest explicit JSON-schema mappings recursively and Pydantic schema classes through static
  class dictionaries; refuse a schema carrier that reaches the original/frozen executable while
  preserving the identity of an ordinary attested Pydantic schema;
- re-snapshot the frozen executable source on each call, rebuilding `_ToolFacts` from the plan's
  immutable metadata so state that legitimately changed between calls remains visible without
  retaining or re-reading the original.

- [ ] **Step 4: Run GREEN and compatibility pins**

Run: `uv run pytest -q tests/integrations/langgraph/tools/test_tool_substitution.py -k 'publishes or signature_and_derived_schema or signature_default or args_schema_carrier or registry_lifecycle or truthfully_wrapped or variadic_decorator or ordinary_callable'`

Expected: PASS.

### Task 2: Guard state-to-implementation closure changes

**Files:**
- Modify: `src/zeroth/integrations/langgraph/_tool_execution.py`
- Modify: `src/zeroth/integrations/langgraph/_tool_wrappers.py`
- Modify: `tests/integrations/langgraph/tools/test_tool_substitution.py`

- [ ] **Step 1: Write failing sync/async state-cell tests**

Create a body closing over `None`; have the classifier and decision client separately replace
that cell with a function. Assert `ToolGovernanceError` is raised after authorization but before
the replacement executes. Retain the existing counter/state positive controls.

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/integrations/langgraph/tools/test_tool_substitution.py -k 'state_cell and implementation'`

Expected: FAIL because shared state cells are not reclassified before execution.

- [ ] **Step 3: Implement guarded snapshots**

Add a frozen internal `FrozenCallableSnapshot(body, state_cells)` record and an internal
`snapshot_guarded_callable()` entry point. Keep `snapshot_callable()` as the compatibility
adapter returning `.body` for callers that only need a callable. Thread one collector through
`_frozen_implementation`, `_frozen_function`, `_frozen_cell`, partials, bound methods, and
callable objects so nested implementation captures merge into the same identity-deduplicated
cell tuple. Record only cells shared because their snapshot-time value was not implementation.

Wire call sites explicitly:

- `_bound_method_body` returns a guarded record to `snapshot_tool`;
- `snapshot_tool` stores callable bodies plus per-slot state-cell tuples;
- `_describe_callable` / the callable-specific plan use the guarded record;
- `_declared_signature` continues to use `snapshot_callable(...)` directly (or may explicitly
  use `snapshot_guarded_callable(...).body`, but never `.body` on the compatibility result);
- `execute_snapshot` and `aexecute_snapshot` check only the cells for the selected body slot;
- both registered plain-callable sync/async dispatch helpers run the same check inside the
  authorized continuation immediately before invoking their selected body.

Add a pre-execution helper that raises `ToolGovernanceError` if any recorded cell now holds
`_is_implementation(value)`.

- [ ] **Step 4: Run GREEN plus state positives**

Run: `uv run pytest -q tests/integrations/langgraph/tools/test_tool_substitution.py -k 'state_cell or keeps_state or closes_over_a_helper'`

Expected: PASS.

### Task 3: Delete inner framework execution

**Files:**
- Modify: `src/zeroth/integrations/langgraph/_tool_execution.py`
- Modify: `src/zeroth/integrations/langgraph/_tool_wrappers.py`
- Modify: `tests/integrations/langgraph/tools/test_tool_substitution.py`
- Modify: `tests/integrations/langgraph/tools/test_tool_wrappers.py`

- [ ] **Step 1: Write failing post-decision framework tests**

Add sync/async tests proving an ambient config subclass's `.copy()` is not called after the
decision and cannot mutate shared arguments. Add process-global configure-hook tests proving
the hook may observe the outer governed span but cannot run between policy and body or mutate
the authorized input. Update the nested-operation test to prove genuine nested LangChain work
inherits the outer run handler.

Add an explicit dispatch matrix:

| captured body | entry point | expected execution |
|---|---|---|
| sync | sync | direct on caller thread |
| sync | async | LangChain executor fallback |
| native async | async | direct await |
| async-only | sync | preserve the existing typed/no-sync-body failure |

- [ ] **Step 2: Write failing output/error compatibility tests**

Change the response-format expectation so the outer wrapper carries
`content_and_artifact`. Add sync/async `ToolException` tests for boolean and callable
`handle_tool_error`, verifying exactly-once handling, and retain an ordinary-exception case.

- [ ] **Step 3: Run RED**

Run: `uv run pytest -q tests/integrations/langgraph/tools/test_tool_substitution.py tests/integrations/langgraph/tools/test_tool_wrappers.py -k 'ambient or configure_hook or nested or response_format or handle_tool_error or dispatch_matrix or async_only'`

Expected: FAIL on inner `invoke/ainvoke`, stale nested-callback expectation, and outer fields.

- [ ] **Step 4: Implement direct snapshot execution**

In `_tool_execution.py`, replace `executing_tool` with direct helpers:

```python
def execute_snapshot(snapshot, args, kwargs):
    body, state_cells = _snapshot_body_with_state(snapshot, "func", "_run")
    if body is None:
        raise ToolGovernanceError(...)
    refuse_state_cell_escalation(state_cells)
    return body(*args, **kwargs)

async def aexecute_snapshot(snapshot, args, kwargs):
    body, state_cells = _snapshot_body_with_state(snapshot, "coroutine", "_arun")
    if body is not None:
        refuse_state_cell_escalation(state_cells)
        return await body(*args, **kwargs)
    return await run_in_executor(None, execute_snapshot, snapshot, args, kwargs)
```

Use LangChain's executor helper for sync-only async fallback. In `_tool_wrappers.py`, call
these helpers directly inside `guard_tool_call` / after `authorize_tool_call`; delete
`_callback_free_config`, `_delegate_input`, and all inner `invoke/ainvoke` construction.
Move exact-type-gated `response_format` and `handle_tool_error` into `_carried_fields`; keep
delegate callbacks excluded.

Delete the obsolete inner-executor surface completely: `executing_tool`, `_adapted`,
`_adapted_async`, `ToolSnapshot.carried`, `_CARRIED_FIELDS`, the `StructuredTool` import, and
their stale module documentation. Preserve the current async-only-via-sync failure type/message
unless the RED compatibility test establishes a deliberate typed replacement.

- [ ] **Step 5: Run GREEN and focused conformance**

Run: `uv run pytest -q tests/integrations/langgraph/tools/test_tool_substitution.py tests/integrations/langgraph/tools/test_tool_wrappers.py`

Expected: PASS.

### Task 4: Documentation and release metadata

**Files:**
- Modify: `docs/how-to/cookbook/govern-langgraph-tools.md`
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `frontend/app/lib/version.ts`
- Modify: `tests/docs/test_release_metadata.py`

- [ ] **Step 1: Update the cookbook**

Document one outer callback tree, direct frozen-body execution, excluded tool-attached
callbacks, and normal outer-context inheritance for genuine nested LangChain operations.

- [ ] **Step 2: Bump version to `0.13.14.8`**

Add the release note and update all five synchronized version declarations.

- [ ] **Step 3: Verify docs metadata and lint**

Run: `uv run pytest -q tests/docs/test_release_metadata.py`

Run: `uv run ruff check src tests`

Expected: PASS.

### Task 5: Full verification and closeout

**Files:**
- Update: `.autopilot/zer-6/checks.tsv` (ignored run evidence)
- Update: `.autopilot/zer-6/PROOF.md` and `.autopilot/zer-6/STATE.md` (ignored evidence)

- [ ] **Step 1: Run focused and integration suites**

Run: `uv run pytest -q tests/integrations/langgraph/tools/test_tool_substitution.py`

Run: `uv run pytest -q tests/integrations/langgraph`

Expected: PASS.

- [ ] **Step 2: Run full suite and full quality gate**

Run: `uv run pytest -q`

Run: `AUTOPILOT_GATE_FULL=1 AUTOPILOT_GATE_BASE=f8293e12 bash ~/.claude/skills/autopilot/assets/quality-gate-hook.sh`

Expected: PASS.

- [ ] **Step 3: Refresh the code graph and run change review**

Run graph incremental update against `f8293e12`, then `detect_changes`, affected flows, and
tests-for checks. Resolve in-scope gaps.

- [ ] **Step 4: Run one final read-only audit**

Provide the approved spec, this plan, prior audit findings, diff, and verification evidence.
Require `VERDICT: APPROVE` before marking ZER-6 done.

- [ ] **Step 5: Commit and update tracking**

Commit the implementation and evidence-backed release metadata. On approval, mark T-2 done,
update `PROOF.md` / `STATE.md`, comment on Jira ZER-6, and transition it out of In Progress.
Do not push or open a PR unless the user separately requests it.
