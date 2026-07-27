# ZER-6 Final Enforcement Closure Design

## Goal

Close the four high-severity findings from cycle-6 audit without adding another
attribute blacklist or changing ZER-6's public adoption surfaces.

## Security invariant

After policy authorizes a normalized tool action, no caller-controlled callback,
configuration hook, mutable implementation reference, or published wrapper handle
may select a different body or change the authorized arguments before that body
starts. A shape that cannot meet this invariant is refused with a typed governance
error.

## Architecture

### Source-free callable plans

Plain callables are snapshotted at wrap time. Their governed wrapper closes over
only an opaque, non-callable token. A module-private registry maps that token to a
plan whose execution source is the frozen callable, and a finalizer removes the
entry when the wrapper is collected. Neither the original nor the frozen executable
is therefore reachable through the wrapper's closure, defaults, annotations,
signature, or attribute mapping.

Annotation values are admitted only from an explicit set of recursively attestable
shapes: safe builtin/type atoms and typing constructs whose origin and arguments are
themselves attestable. Exact builtin containers are traversed. Wrapping is refused
if the graph contains the original or frozen executable, or if an annotation is a
custom/opaque object or class whose publication cannot be attested without trusting
its attributes or dispatch. The wrapper is built without `functools.wraps`; only
attested annotations and string metadata are copied explicitly, preserving ordinary
LangChain schema inference without copying arbitrary source attributes.

The frozen callable becomes the registry plan's canonical target for per-call
description, signature binding, resolver inputs, and execution. Mutating an
unrelated reference to the original cannot alter the governed callable. Arbitrary
reflection into module globals is outside the boundary: code able to read private
module state can already import and call private implementation functions. The
falsifiable boundary is that recursively traversing the returned wrapper's owned
attributes, defaults, annotations, signature, and closure values yields no
unguarded executable or plan.

### Guarded frozen bodies

Snapshotting records every closure cell deliberately shared because it contained
state rather than implementation. Immediately after authorization and immediately
before execution, the wrapper verifies that none of those cells now contains an
implementation value. A state-to-implementation transition fails closed. Cells
that remain state stay shared, preserving counters, memoization, clients, and other
legitimate stateful behavior.

### Direct post-decision execution

The governed `BaseTool` remains the only LangChain execution layer. Its inherited
outer `run`/`arun` performs input validation and emits the caller-visible callback
span before governance. Once authorization succeeds, `_run`/`_arun` directly call
the frozen snapshot body; they do not call `BaseTool.invoke`, `run`, `ainvoke`, or
`arun` on an inner tool.

This removes both post-decision framework paths:

- inherited `RunnableConfig` values are no longer copied by `ensure_config`; and
- process-global configure hooks cannot create a second callback manager.

The outer wrapper carries the source tool's `response_format` and
`handle_tool_error`, because it becomes the single layer that formats output and
handles body failures. LangChain therefore formats content/artifact output and
handles `ToolException` exactly once after the direct body returns. Existing
async-fallback behavior is preserved by the direct executor: a native coroutine is
awaited; a sync-only body reached asynchronously runs in an executor.

The direct body runs in the outer tool's existing child configuration. Nested
LangChain operations initiated by the body consequently inherit the outer run's
handlers normally. Process-global configure hooks may observe the outer span and
those genuinely nested operations, but no callback manager is constructed between
authorization and entry into the authorized body, so they have no post-decision
opportunity to rewrite that call.

## Data flow

1. LangChain parses and validates the call through the governed outer tool.
2. Zeroth snapshots/describes the source and normalizes the exact arguments.
3. Policy authorizes or refuses the action.
4. Zeroth checks recorded shared state cells for implementation escalation.
5. Zeroth directly invokes the frozen body once.
6. The outer LangChain tool formats the result and emits its end/error callback.

Plain callables follow the same steps without LangChain's outer parsing layer.

## Error handling

- Direct or transitive wrapper publication of an unguarded executable is prevented;
  source references nested in annotations are refused during wrapping.
- State cells that become executable after snapshot raise a typed governance error
  before the substituted implementation runs.
- Direct body exceptions continue through the governed outer tool's normal error
  path; policy denial and policy failure semantics do not change.
- Unsupported execution shapes remain fail-closed.

## Verification

Add RED-first sync and async regressions proving:

1. recursively traversing callable annotations, signatures, owned attributes,
   defaults, and closure values exposes no original/frozen callable or plan, and no
   extracted callable can execute the tool body without governance; include direct,
   nested-container, typing-metadata, and custom attribute-bearing annotation cases;
2. classifier/client mutation of state cells into functions is refused before the
   substituted body executes;
3. malicious ambient config `.copy()` methods cannot run after authorization;
4. process-global configure hooks may observe the outer span and real nested work
   but cannot mutate the authorized input after the policy decision;
5. `handle_tool_error` boolean and callable handlers process sync and async
   `ToolException` exactly once, while ordinary exceptions retain prior behavior;
6. response formatting, schema inference, stateful bodies, sync/async fallbacks,
   denial, and exactly-once execution remain compatible.

Then run the focused substitution suite, LangGraph integration suite, repository
Ruff, full pytest suite, autopilot checks, full quality gate, and one final read-only
audit.

## Documentation

Update the cookbook's known divergences: one outer callback tree fires per governed
call, the frozen body executes directly, tool-attached callbacks are not copied,
nested LangChain work inherits the outer run context normally, and governance audit
is the supported decision/execution observability path.

## Non-goals

- No sandbox against arbitrary host-process memory mutation.
- No global LangChain registry mutation or temporary monkey-patching.
- No deep copy of ordinary tool state.
- No new public API or unrelated refactor.
