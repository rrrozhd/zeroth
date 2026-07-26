# Govern LangGraph tool calls

## What this recipe does
Puts every tool call a LangGraph or `create_agent` application makes through
Zeroth's tool-enforcement core, so each call is allowed, denied, or suspended for
human approval **before** the tool body runs — and each decision is recorded on
the typed audit fields.

There are two install surfaces and they share one enforcement core:

- `govern_tools(tools, ...)` — returns governed twins of a raw tool list. Nothing
  about the originals is mutated: no attribute is written back, `.func` and
  `.coroutine` are never rebound, and the supplied container is copied.
- `ZerothMiddleware(...)` — an `AgentMiddleware` for `create_agent`. LangChain
  hands the tool's execution in as a `handler` callback, so a denial or an
  approval is literally "the handler was never called". **Install it last** — see
  [Middleware nesting order](#middleware-nesting-order).

## When to use
- You already have a LangGraph app or a `create_agent` agent and want tool calls
  decided by policy without rewriting the tools.
- You need an audit record per tool decision, carrying the tool on
  `NodeAuditRecord.tool_calls` and the approval on `approval_actions`.
- You need a side-effecting tool to pause for human approval mid-run.

## When NOT to use
- You want the model to *see* the denial and try something else. Governance
  raises `PolicyViolation`; it never renders a verdict as an error `ToolMessage`.
  Catch the exception in your own middleware, outside governance, if you want it
  fed back to the model.
- You want cumulative graph-level enforcement. Tool enforcement is not that — see
  [What this does not claim](#what-this-does-not-claim).

## Install

The integration's dependencies are an opt-in extra:

```bash
pip install "zeroth-core[langgraph]"
```

That brings `langchain` (which ships `langchain.agents`, the middleware base
class) and `langgraph` (which ships `langgraph.types.interrupt`, the approval
pause seam). Both are still imported **lazily** — `import
zeroth.integrations.langgraph` pulls in neither, so installing without the extra
leaves the rest of the package working and only `govern_tools`, `GovernedTool`
and `ZerothMiddleware` unavailable. The extra is how you opt in, not a licence to
import eagerly. (It is deliberately not part of `zeroth-core[all]`, which is the
headless runtime bundle.)

## Recipe

### Surface 1 — govern a raw tool list

```python
from zeroth.integrations.langgraph import (
    SideEffectClass,
    ToolGovernanceContext,
    govern_tools,
)

context = ToolGovernanceContext(
    tenant_id="tenant-a",
    principal_id="principal-1",
    run_id="run-1",
    thread_id="thread-1",      # approval needs a thread to resume into
    correlation_id="corr-1",
)

governed = govern_tools(
    tools,
    context=context,
    client=my_decision_client,                       # None denies every call
    side_effect=lambda tool: SideEffectClass.READ_ONLY,
    audit=audit_delivery_queue,
)
```

The returned wrappers go wherever the originals went — a `ToolNode`, a
`StateGraph`, a `bind_tools` call — and answer to the same interfaces.

Everything in this recipe imports from `zeroth.integrations.langgraph` — the
install surfaces *and* the vocabulary. The types are public because the
enforcement path gates on **exact type**: a verdict counts only when it is
exactly a `ToolDecision` carrying exactly a `ToolDecisionKind`, so there is no
duck-typed way to write a decision client and the types are mandatory rather
than optional. The public surface is:

| Group | Names |
| --- | --- |
| Install surfaces | `govern_graph`, `govern_tools`, `GovernedTool`, `ZerothMiddleware` |
| Describe a call | `ToolGovernanceContext`, `ToolIdentity`, `ToolAction`, `SideEffectClass` |
| Decide a call | `ToolDecisionClient`, `ToolDecision`, `ToolDecisionKind`, `FailClosedToolDecisionClient`, `UnknownSideEffectPolicy`, `ToolAuditSubmitter` |
| Typed refusals | `ToolGovernanceError`, `PolicyViolation`, `GovernanceContextError`, `UnstableToolIdentityError`, `ApprovalRequiresThreadError` |
| Read the surface | `ToolInventory`, `ToolInventoryEntry`, `InventoryCoverage`, `ToolInventoryMatch`, `ToolEnforcementReport`, `record_tool_inventory`, `report_tool_enforcement`, `match_tool_inventory`, `attest_complete_inventory` |

Writing a decision client needs nothing beyond that group:

```python
from zeroth.integrations.langgraph import (
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
)


class MyDecisionClient:
    """Structurally satisfies ToolDecisionClient — no base class to inherit."""

    def decide(
        self, action: ToolAction, context: ToolGovernanceContext
    ) -> ToolDecision:
        if action.identity.name == "search":
            return ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="unknown_error")
        return ToolDecision(kind=ToolDecisionKind.DENY, reason_code="policy_violation")
```

The action arrives already normalized — identity pinned, arguments canonical,
principal injected — so a client never normalizes, never fingerprints, and is
never handed raw call material. That is why the normalizers and the fingerprint
digests stay private: identities are *derived*, not caller-asserted, and a
fingerprint the caller supplies is a claim the substituting party is best placed
to make. `guard_tool_call` / `authorize_tool_call` are private for a different
reason — enforcement lives in exactly one place, and a supported way to re-enter
it is a second surface for the fail-closed rules to drift in.

### Surface 2 — govern an agent's tool calls

```python
from langchain.agents import create_agent

from zeroth.integrations.langgraph import ZerothMiddleware, govern_graph

agent = create_agent(model, tools=tools, middleware=[ZerothMiddleware(context=context)])
graph = govern_graph(agent)
```

Composed in one line, that is:

```python
govern_graph(create_agent(..., middleware=[ZerothMiddleware()]))
```

`ZerothMiddleware()` with no `context` is a *fail-closed* installation: the
principal is injected and never discovered, so an agent governed without a
context refuses every call rather than running unattributed. Pass
`context=` — or a zero-argument callable returning one per call — for a
working install.

## Middleware nesting order

**`ZerothMiddleware` must be the LAST `wrap_tool_call` middleware in the list.**
This is a requirement, not a style preference.

```python
create_agent(model, tools=tools, middleware=[retry, tracing, ZerothMiddleware(context=context)])
#                                             everything else first ─┘  governance last ─┘
```

LangChain composes `wrap_tool_call` middleware **first-defined-outermost**. For
`create_agent(..., middleware=[A, ZerothMiddleware(...), B])` a tool call enters
`A`, then governance, then `B`, and unwinds in reverse. Pinned by
`test_three_middleware_nest_first_defined_outermost`, which asserts the exact
sequence `["a:enter", "z:decide", "b:enter", "b:exit", "a:exit"]`.

Two failures follow from nesting a middleware *inside* governance:

- **Rewritten arguments.** An inner middleware can call
  `handler(request.override(tool_call=modified))`, and the tool then runs with
  arguments the decision was never made about.
- **Undecided executions.** LangChain hands each layer a handler its own body may
  call as many times as it likes — `_chain_tool_call_wrappers.compose_two` in
  `langchain/agents/factory.py` says so in a comment ("Outer can call call_inner
  multiple times"), and LangChain's shipped `ToolRetryMiddleware` does exactly
  that (`langchain/agents/middleware/tool_retry.py`). A retry nested inside
  governance therefore runs the tool body N times against **one** decision and
  **one** audit record.

Installed last, the same retry re-enters `wrap_tool_call` on every attempt, so
**every physical tool execution gets its own decision and its own audit record**.
Pinned by `test_an_outer_retry_gets_a_decision_and_a_record_per_physical_execution`
and its async twin, which count executions of the tool function itself — not
handler calls.

### "Exactly once" means the handler, not the body

`wrap_tool_call` runs its downstream exactly once per decision, outside any loop,
and never retries. That is a statement about the *handler*. How many times the
tool body runs beneath the handler is whatever the layers nested inside
governance do, and the two numbers coincide only when governance is innermost.

### Why this is a contract and not a check

Nothing in the middleware validates its own position, because nothing supported
can. `AgentMiddleware` exposes no hook that receives the middleware list,
`create_agent` composes the chain into a closure the middleware never sees, and
the only observable difference between an innermost install and a nested one is
whether the handler is the tool executor or LangChain's private
`compose_two.<locals>.call_inner`. Distinguishing those means reading another
library's local closures — it would break silently when LangChain refactors, and
a position guard that silently stops guarding is worse than a documented contract
with tests behind it. The limitation is pinned by
`test_a_retry_nested_inside_governance_runs_the_body_undecided`, so it can never
quietly turn back into a claim.

A wrong order does **not** weaken denial: a refused call raises before the nested
layer is reached, so it executes zero times either way
(`test_a_retry_nested_inside_governance_still_never_runs_a_denied_call`). What a
wrong order loses is the per-execution decision and the per-execution record.

## What this does not claim

**Middleware-only integration cannot claim cumulative graph enforcement without a
matching graph attestation.** Tool-level enforcement is not graph-level or
cumulative enforcement, and installing `ZerothMiddleware` does not make it so.

The mechanics:

- A run reports `ENFORCED` only when its capability evidence has
  `governance_level is ENFORCED` **and** `tool_manifest_complete` is true
  (`src/zeroth/core/langgraph_gateway/capabilities.py:86-90`).
- Nothing in `src/` mints evidence with `tool_manifest_complete=True`. The field
  exists on `RunCapabilityEvidence` with a default of `False`
  (`src/zeroth/core/langgraph_gateway/models.py:132`) and the only other
  references are the capability check above and this package's own docstrings.
- This integration documents in five places that it never promotes a run above
  `admission` (FA5) — once per surface: `_wrapper.py:79` and `_wrapper.py:331`
  (`govern_graph`), `__init__.py:15` (`govern_graph`), `__init__.py:35`
  (`govern_tools`) and `__init__.py:47` (`ZerothMiddleware`).

So a tool-only run reports `observed` with `partial` coverage, plus an explicit
list of the tools actually governed —
`report_tool_enforcement(record_tool_inventory(governed))` returns a
`ToolEnforcementReport` whose `level` is `observed` when at least one governed
tool is present and `admission` when none is, whose `coverage` is `partial`, and
whose `enforced_tools` names exactly what was governed. `govern_tools` takes no
coverage parameter on purpose: declaring a complete inventory requires an
explicit expected tool list whose fingerprints match, which is
`attest_complete_inventory`'s job, and even a complete inventory is not the
signed run evidence `ENFORCED` needs.

### The same report from a middleware-only install

`ZerothMiddleware` wraps no tool — the agent keeps the originals and hands one in
per call — so there is no governed wrapper to record an inventory from. Declare
the tools instead:

```python
guard = ZerothMiddleware(context=context, expected_tools=tools)
agent = create_agent(model, tools=tools, middleware=[guard])

report = guard.enforcement_report()
report.level           # GovernanceLevel.OBSERVED  (ADMISSION when none declared)
report.coverage        # InventoryCoverage.PARTIAL (always)
report.enforced_tools  # ("search", "write_row")
report.level_term      # "observed" — the plain str audit metadata must carry
```

`expected_tools` is **not injected**: it is not added to `middleware.tools`, not
handed to the agent, and not wrapped. It is pinned through the same
`_describe_base_tool` a live call is described through, so an inventory entry
carries the fingerprint the decision is actually made under; `guard.tool_inventory`
exposes it for `match_tool_inventory` against a declared identity list.

Two properties this report has by construction:

- **It can never be `enforced`.** There is one `report_tool_enforcement` with no
  `ENFORCED` branch, and the middleware mints no capability evidence of any kind
  (`test_the_middleware_report_can_never_be_enforced`). Coverage stays `partial`:
  a middleware never sees the agent's tool list, and a declaration is not a
  discovery.
- **The inventory gates nothing.** A call naming an undeclared tool is decided
  exactly as any other call is — the enforcement core is the only thing that
  refuses a call
  (`test_an_undeclared_tool_is_still_decided_and_the_inventory_gates_nothing`).

An unusable declaration fails at construction rather than at report time: a
non-`BaseTool` entry, an unusable name, or two tools sharing one name all raise
`UnstableToolIdentityError` from `ZerothMiddleware(...)`.

## Compatibility matrix

Both wrapping surfaces preserve the tool's interface and invoke the underlying
body exactly once per allowed call. (Per *call* — a layer above may make several
calls; see [Middleware nesting order](#middleware-nesting-order).) Every
documented cell has a named test in
`tests/integrations/langgraph/tools/test_tool_wrappers.py`:

| Target | Sync/async | `args_schema` | Test |
| --- | --- | --- | --- |
| `BaseTool` | sync | present | `test_cell_base_tool_sync_with_args_schema_preserves_interface_and_invokes_once` |
| `BaseTool` | sync | absent | `test_cell_base_tool_sync_without_args_schema_preserves_interface_and_invokes_once` |
| `BaseTool` | async | present | `test_cell_base_tool_async_with_args_schema_preserves_interface_and_invokes_once` |
| `BaseTool` | async | absent | `test_cell_base_tool_async_without_args_schema_preserves_interface_and_invokes_once` |
| plain callable | sync | present | `test_cell_plain_callable_sync_with_args_schema_preserves_interface_and_invokes_once` |
| plain callable | sync | absent | `test_cell_plain_callable_sync_without_args_schema_preserves_interface_and_invokes_once` |
| plain callable | async | present | `test_cell_plain_callable_async_with_args_schema_preserves_interface_and_invokes_once` |
| plain callable | async | absent | `test_cell_plain_callable_async_without_args_schema_preserves_interface_and_invokes_once` |

Cross-surface parity — that `govern_tools` and `ZerothMiddleware` share one
decision client, one typed-exception set, one audit projection and one interrupt
schema — is driven from a single shared scenario table in
`tests/integrations/langgraph/tools/test_surface_parity.py`.

**The eight matrix cells and the parity table run in different tiers.** Get this
wrong and you run the wrong suite:

- **The eight `test_cell_*` cases run in the default (base) tier.** They are in
  `test_tool_wrappers.py`, which carries no `langgraph_conformance` marker and no
  `importorskip`: `langchain-core` is a *core* dependency, and the pause seam is
  injected, so nothing there needs `langchain.agents` or `langgraph`. A marker
  would only have got them deselected. Run them with the ordinary suite:
  ```bash
  uv run pytest tests/integrations/langgraph/tools/test_tool_wrappers.py
  ```
- **The cross-surface parity table is Tier A.** `test_surface_parity.py` (like
  `test_middleware.py`) drives `create_agent`, so it `importorskip`s
  `langchain.agents` and is marked `langgraph_conformance`, which the default
  `addopts` deselects. It needs the `gateway-conformance` dependency group — or
  the `langgraph` extra — and an explicit run:
  ```bash
  uv run pytest -o addopts= -m langgraph_conformance tests/integrations/langgraph
  ```

A conformance run that reports everything skipped means the dependencies are
absent, not that the tier passed.

## Known divergences

Two things are deliberately *not* identical between a governed wrapper and the
tool it wraps. Both are known and neither is a governance gap.

### `response_format` is not carried

The wrapper reads `"content"` even when the delegate reads
`"content_and_artifact"`. This is an attribute divergence with behavioral
**equivalence**: the wrapper forwards the whole tool call, id included, so the
delegate builds its own `ToolMessage`, and the wrapper's output formatting passes
a `ToolOutputMixin` straight through. Carrying `response_format` made the wrapper
re-format already-formatted output and reject the delegate's own `ToolMessage`
for not being a two-tuple. Backed by
`test_content_and_artifact_survives_the_wrapping_and_the_artifact_is_not_dropped`.

The split is by *who reads the field*:

- **Carried**: `return_direct`, `tags`, `metadata`, `handle_validation_error`.
  The agent loop reads `return_direct` off the tool object it was handed — the
  wrapper — and never off the delegate it cannot see. The wrapper parses input
  first, with the same schema, so it is the only layer that can fail validation.
- **Not carried**: `callbacks`, `handle_tool_error`, `response_format`. The
  delegate is the layer that runs, formats, and therefore fails.

### Two callback trees fire per governed call

The wrapper's `run()` and the delegate's `run()` each emit `on_tool_start` /
`on_tool_end`, so a callback-based observer sees two tool spans for one governed
call. This is telemetry divergence, not a governance gap: the tool body still
executes exactly once per governed call, and that call is still recorded exactly
once.

## See also
- [Block a tool call via policy](policy-block.md)
- [Sandbox a tool call](sandbox-tool.md)
- [Add an approval step](approval-step.md)
- [Concept: guardrails](../../concepts/guardrails.md)
- [Concept: audit](../../concepts/audit.md)
