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
- `ZerothMiddleware(...)` — an `AgentMiddleware` for `create_agent`. It carries no
  decision of its own: it substitutes a governed twin of `request.tool` — built by
  the same `govern_tools` machinery, per call — into the request it hands
  downstream, and LangChain's `ToolNode` executes *that*. So the two surfaces are
  literally one implementation, and the decision is made about the arguments the
  body receives, after `BaseTool` has validated, coerced and defaulted them.
  **Install it last** — see [Middleware nesting order](#middleware-nesting-order).

A denial or an approval therefore travels back **up through** the handler: it is
raised inside the tool's execution, not before it is reached. "The call was
refused" is observable as *the tool body ran zero times*, never as a handler
count.

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
`A`, then governance, then `B`, and unwinds in reverse. The substitution happens
at governance's own position, so `A` is handed the raw tool and `B` is handed the
governed twin. Pinned by `test_three_middleware_nest_first_defined_outermost`,
which asserts the exact sequence
`["a:enter:raw", "b:enter:governed", "z:decide", "b:exit", "a:exit"]`.

One failure follows from nesting a middleware *inside* governance, and it is
severe:

- **Un-substitution.** An inner middleware receives the governed twin and can hand
  its own downstream something else — `handler(request.override(tool=raw))`, or
  the same move on `tool_call`. Because the substitution *is* the enforcement, the
  raw tool then runs with **no decision and no audit record at all**. Nothing
  raises and nothing is recorded, so neither a deny count nor a record count can
  see it. Pinned by
  `test_a_middleware_nested_inside_governance_can_strip_the_governed_twin`.

Anything that rewrites a request therefore belongs *outside* governance, where the
substitution happens after it.

### Retries are no longer a hole, either side of governance

LangChain hands each layer a handler its own body may call as many times as it
likes — `_chain_tool_call_wrappers.compose_two` in `langchain/agents/factory.py`
says so in a comment ("Outer can call call_inner multiple times"), and LangChain's
shipped `ToolRetryMiddleware` does exactly that
(`langchain/agents/middleware/tool_retry.py`). Every one of those calls now
reaches the governed twin, so **every physical tool execution gets its own
decision and its own audit record** whichever side of governance the retry sits
on. Pinned by
`test_an_outer_retry_gets_a_decision_and_a_record_per_physical_execution`, its
async twin, and `test_a_retry_nested_inside_governance_is_decided_per_execution_too`
— all of which count executions of the tool function itself, not handler calls.

This retires a limitation earlier revisions documented: a retry nested inside
governance used to run the body N times against one decision and one record.

### Why this is a contract and not a check

Nothing in the middleware validates its own position, because nothing supported
can. `AgentMiddleware` exposes no hook that receives the middleware list and
`create_agent` composes the chain into a closure the middleware never sees.
Distinguishing an innermost install from a nested one means reading another
library's local closures — it would break silently when LangChain refactors, and a
position guard that silently stops guarding is worse than a documented contract
with tests behind it.

A wrong order does **not** weaken denial on its own: a nested layer that simply
passes the request through still executes the governed twin, so the refusal still
holds and the body still runs zero times
(`test_a_denial_now_reaches_the_middleware_nested_inside_governance`). What it now
does mean is that such a layer *observes* calls governance goes on to refuse —
and, if it rewrites the request, defeats governance entirely.

### Pick one surface per tool list

Passing `govern_tools(...)` output to `ZerothMiddleware` is a configuration error
and is **refused at every call** with `UnstableToolIdentityError`: `GovernedTool`
overrides `_to_args_and_kwargs`, which the entry-hook ban refuses, exactly as
`govern_tools` refuses a tool it already wrapped. Earlier revisions accepted this
silently and produced two decisions and two audit records for one physical
execution. Pinned by `test_an_already_governed_tool_is_refused_by_the_middleware_too`
and `test_an_already_governed_tool_is_refused_by_govern_tools`.

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

### Tool identity covers code, not instance configuration

**A reconfigured instance is not a detected substitution.** Identity is re-derived
on every call from the tool's *implementation* — a canonical projection of the
`_run`/`_arun` (or `func`/`coroutine`) code object — together with its declared
name, description and argument schema. The instance's own configuration is not in
it.

So these two are identity-identical, and a policy that authorized the first will
authorize the second:

```python
class HttpTool(BaseTool):
    endpoint: str
    def _run(self, path: str) -> str: ...

HttpTool(endpoint="https://good.example")   # same fingerprint
HttpTool(endpoint="https://evil.example")   # as this one
```

The same holds for configuration captured in a closure: `make_tool(config_a)` and
`make_tool(config_b)` share an identity, because `_run` is read off `type(tool)`
and both closure cells and instance attributes reduce to type names.

**The trade-off is forced, not an oversight.** Because identity is re-derived and
compared on *every* call, digesting mutable bound state would make a tool that
counts its own invocations or caches an HTTP client refuse its own second call as
a substitution of its first — fail-closed on correct code, on every long-running
agent. Pinned by
`test_a_tool_that_carries_state_keeps_its_identity_across_hundreds_of_calls`.

**What to do when configuration is what needs governing.** Put it somewhere the
policy actually sees:

- Pass it as a tool *argument*. Arguments are canonicalized into the action and
  decided per call, so an endpoint or a file root arrives at your client.
- Pin it in `contract_ref`, which is resolved per call and carried on the action.
- Give each configuration its own tool with its own implementation, so the
  fingerprints genuinely differ.

Do **not** rely on the fingerprint to notice that an endpoint, a credential or a
file root changed. Closing this properly requires configuration to become part of
the declared surface — a per-tool identity declaration — which is a new public API
contract rather than a fix to the projection, and is tracked as its own issue.

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

## What governance refuses to wrap

Two independent narrowings live here. The first is about a tool whose *entry path*
cannot be read; the second is about an *argument value* that cannot be
represented. Neither can be lifted by configuration.

### A tool that overrides a pre-body entry point

**A tool that overrides a pre-body entry point is refused, not governed.** This
is a deliberate narrowing, and it will reject some tools that wrapped fine
before `0.13.12`.

Since `0.13.13` this applies to **both** install surfaces. `ZerothMiddleware`
builds its governed twin through the same machinery, so a tool it would once have
decided-then-run is now refused at the call, with the same
`UnstableToolIdentityError`
(`test_a_tool_that_overrides_a_pre_body_entry_point_is_refused_on_this_surface_too`).

`govern_tools` guarantees that the arguments policy was asked about are the
arguments the body receives. It gets that by parsing the call once, against the
delegate's own `args_schema`, and then driving the delegate through a twin whose
validation stage is a pass-through. That guarantee holds only when the delegate
reaches its body through `BaseTool`'s own machinery. A subclass that overrides
any of

`_parse_input`, `_to_args_and_kwargs`, `invoke`, `ainvoke`, `run`, `arun`

re-derives the call *after* the decision, inside a hook the wrapper cannot see
past — policy authorizes `{"query": "safe"}` and the body runs `"danger"`. Those
tools now raise `UnstableToolIdentityError` at `govern_tools`, and again before
any execution if the class gains an override afterwards.

`langchain-core`'s own `BaseTool` and `StructuredTool` implementations are
permitted, and `StructuredTool` is what the `@tool` decorator produces for both
single- and multi-argument functions — so the ordinary way of writing a tool is
unaffected. What is affected:

- a hand-written `BaseTool` subclass that overrides one of those six hooks
  (overriding `_run` / `_arun` is the normal case and stays fine);
- `langchain_core.tools.Tool`, the legacy single-input class, which overrides
  `_to_args_and_kwargs`;
- a governed tool passed back through `govern_tools` a second time.

If you hit this, move the logic out of the entry hook and into `_run` / `_arun`,
or wrap the underlying function as a plain callable. The refusal is the
fail-closed direction on purpose: it can be lifted once there is a neutral
execution adapter that bypasses those hooks, and until then a tool whose entry
path is unreadable is a tool whose authorized call and executed call cannot be
shown to be the same call.

### An injected argument whose value is not canonically representable

**A tool that injects the whole graph state is refused at every call**, on both
surfaces, with

```
ToolGovernanceError: tool argument value is not representable as canonical JSON
```

The rule is *not* "injected arguments are refused". An injected argument goes
through exactly the projection every other argument goes through, so its **value**
decides the outcome:

| Declared argument | Injected value | Outcome |
| --- | --- | --- |
| `Annotated[dict, InjectedState]` | the whole state, whose `messages` are `BaseMessage` objects | **refused** |
| `Annotated[MyStore, InjectedStore]` | a `BaseStore` instance | **refused** |
| `Annotated[str, InjectedState("user_id")]` | a `str` from one state field | **governed** — policy sees `user_id` |
| `Annotated[str, InjectedToolCallId]` | the call id, a `str` | **governed** — policy sees `tool_call_id` |

So the workaround is to **narrow the injection to the slice the tool actually
needs**. `InjectedState("user_id")` is both representable and a better tool
declaration: it is the field the body uses, and it is a field a policy can be
written against.

```python
# Refused: the whole state is not representable.
def search(query: str, state: Annotated[dict, InjectedState]) -> str: ...

# Governed: the one field the body needs, which policy can now deny on.
def search(query: str, user_id: Annotated[str, InjectedState("user_id")]) -> str: ...
```

If a tool genuinely needs an unrepresentable object — a store handle, a client —
pass it through a closure or a resolver seam rather than a tool argument. An
argument is the thing policy decides about; a dependency is not.

**Why the value is refused rather than dropped.** Eliding an argument, or standing
a placeholder in for it, would hand the policy a *different* call from the one
about to run: a policy that denies `path="/etc/shadow"` would be shown a call with
no path and allow it. `_tool_normalize.py` states this as its governing rule, and
it holds for injected arguments for the same reason it holds for model-supplied
ones — the body receives the value either way.

**This changed on the middleware surface in `0.13.13`, and it is the fix, not a
regression.** `govern_tools` has always refused these. `ZerothMiddleware` appeared
to accept them only because it decided from `request.tool_call["args"]` — the raw
model call, which never contains an injected argument at all. The tool then ran on
a value no gate had seen. Deciding the validated call is what made the value
visible, and a visible unrepresentable value is refused.

Pinned by `test_a_tool_with_an_injected_state_argument_is_refused_rather_than_half_decided`
and, for all four rows above driven through both surfaces, by
`test_both_surfaces_decide_an_injected_argument_identically`.

Showing policy the *names* of injected arguments without their values would be a
third option — neither refusing nor eliding. It is deliberately not implemented:
it needs a new `ToolAction` facet rather than a change to the argument projection,
because an argument mapping that mixes decided values with named-only entries is
one a policy cannot read unambiguously.

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
