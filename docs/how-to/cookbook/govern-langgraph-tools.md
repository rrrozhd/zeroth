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
  approval is literally "the handler was never called".

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

## Recipe

### Surface 1 — govern a raw tool list

```python
from zeroth.integrations.langgraph import govern_tools
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolGovernanceContext,
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

`govern_tools` and `ZerothMiddleware` are exported from
`zeroth.integrations.langgraph`. The tool vocabulary types
(`ToolGovernanceContext`, `SideEffectClass`, `ToolDecision`) are still internal
to the integration, so they are imported from their private module for now.

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

LangChain composes `wrap_tool_call` middleware **first-defined-outermost**. For
`create_agent(..., middleware=[A, ZerothMiddleware(...), B])` a tool call enters
`A`, then governance, then `B`, and unwinds in reverse. Pinned by
`test_three_middleware_nest_first_defined_outermost`, which asserts the exact
sequence `["a:enter", "z:decide", "b:enter", "b:exit", "a:exit"]`.

Two consequences:

- Put `ZerothMiddleware` **ahead of** anything that must not observe a refused
  call, and **behind** anything that legitimately rewrites the request first.
- Governance should be the innermost layer that touches the request. A
  middleware nested *inside* it can still call
  `handler(request.override(tool_call=modified))`, and the tool would then run
  with arguments the decision was never made about. Nothing in LangChain
  prevents that; the ordering is the control.

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
- This integration documents in four places that it never promotes a run above
  `admission` (FA5): `_wrapper.py:79`, `_wrapper.py:331`, `__init__.py:15` and
  `__init__.py:35`.

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

## Compatibility matrix

Both wrapping surfaces preserve the tool's interface and invoke the underlying
body exactly once on an allow. Every documented cell has a named test in
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

All of these are Tier A: they need the optional `gateway-conformance`
dependency group and are deselected by default. Run them with
`uv run pytest -o addopts= -m langgraph_conformance tests/integrations/langgraph`.

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
executes exactly once, and the decision is still recorded exactly once.

## See also
- [Block a tool call via policy](policy-block.md)
- [Sandbox a tool call](sandbox-tool.md)
- [Add an approval step](approval-step.md)
- [Concept: guardrails](../../concepts/guardrails.md)
- [Concept: audit](../../concepts/audit.md)
