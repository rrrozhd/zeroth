# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation

- Remove internal implementation specifications, refactor work logs, deferred
  decomposition plans, and task-ledger documents from the user documentation.
- Point the README, documentation site, package metadata, quickstart, security,
  and contribution links at the canonical `rrrozhd/zeroth` repository and live
  `/zeroth/` GitHub Pages site.
- Distinguish the latest PyPI release from the current `main` documentation,
  document the canonical eight-domain backend layout and LangGraph extras, and
  restore current platform concepts to the docs navigation.

## [0.17.0.7] - 2026-08-07

### Added

- Consult the side-effect receipt store on the executable-unit dispatch path:
  a completed operation is replayed from its stored receipt instead of applying
  the effect again, a timeout becomes ambiguous reconciliation work rather than
  a confirmed failure, and the outcome is recorded in the node's audit record.
- Count first execution, replay suppression, ambiguity, reconciliation success
  and failure, and lease-fencing rejection as distinct metrics.

## [0.17.0.6] - 2026-08-07

### Added

- Thread `OperationIdentity` through all three executable-unit invocation
  paths (manifest ref, inline source, and agent mid-loop tool calls) as an
  explicit parameter, so integrations consume it without parsing run metadata.
  Each tool call in an agent turn receives its own call ordinal.

## [0.17.0.5] - 2026-08-07

### Added

- Add `side_effect_operations`: a durable receipt per logical side-effecting
  operation, distinguishing not-started, in-flight, completed, failed, and
  ambiguous outcomes, with convergent duplicate reports and a bounded
  reconciliation path for ambiguous ones.
- Add lease generations. Claiming and reclaiming a run advance the generation,
  renewal is qualified by it, and `commit_fenced` rejects a write from a
  superseded generation.

### Fixed

- Losing a lease now stops the displaced worker's execution instead of only
  logging it, and the worker no longer marks the new owner's run as failed.

## [0.17.0.4] - 2026-08-07

### Added

- Introduce `OperationIdentity`, a stable logical-operation identity for
  side-effecting work, derived so that transport retries, token retries, and
  recovered workers reproduce the same key while distinct operations do not.

## [0.17.0.3] - 2026-08-06

### Fixed

- Correct the orchestration-errors docstring, which claimed the package facade
  imports its collaborators and that raising through it would close an import
  cycle. Executed: the facade resolves lazily, and importing `OrchestratorError`
  through it loads only the errors module. The true reason the module stands
  alone — it imports nothing from `zeroth` — is now what it says.
- Catch duplicated assertions in `else`, `except` and `finally` blocks. The
  vacuity guard walked only each node's `body`, so a repeat in any other
  statement list passed.
- Keep the self-comparison detector conservative around calls. Builtins such as
  `list(iterator)` can consume state and `len(value)` can invoke user code, so
  repeated call expressions are not assumed to produce the same value.

### Changed

- Scope the vacuity guard's docstring to the three rules it actually implements.
  It advertised a fourth it deliberately omits, which is the same defect class
  the guard exists to catch.
- Make the numbers behind that omission reproducible: 477 test functions carry
  no bare `assert`, of which 43 stay unexplained after crediting
  `pytest.raises`, mock `assert_*`, and helper delegates. Regenerate both with
  `scripts/count_assertion_free_tests.py`.
- Scan every Git-tracked Python file when pinning the C901 suppression set, so
  suppressions in scripts, examples, or application code cannot bypass the
  registry merely by living outside `src/` and `tests/`.

## [0.17.0.2] - 2026-08-06

### Fixed

- Replace the assertions the first fix round left vacuous with properties that
  are actually load-bearing, each mutation-tested: the control plane's `ECP_`
  environment prefix instead of re-importing a cached module, and "every zeroth
  module the extras gate names must exist" instead of the absence of one
  hard-coded name.
- Correct public docstrings that a blanket rewrite made legacy-free but false —
  the governed package described itself as retired, the gateway package claimed
  to republish its own objects, and analytics cited a module that does not
  exist.
- Walk the three canonical gateway packages in the domain-classification test.
  It iterated the deleted `zeroth/core/langgraph_gateway` directory, so it
  passed by checking nothing.

### Changed

- Rewrite the vacuity guard against sibling statements instead of line
  distance, and drop its whole-file suppressions. It now catches keyword-form
  empty parametrization, `x == x`, and duplicates separated by a blank line,
  while correctly allowing `f() == f()` and assert-mutate-assert. The
  assertion-free-body rule is deliberately absent: 46 existing tests would trip
  it and every one sampled asserts for real in a way no static rule sees.

## [0.17.0.1] - 2026-08-06

### Fixed

- Close four defects the initial audit found in ZER-25's own conversion: two
  more `parametrize` lists that collected zero cases, a self-comparison and a
  duplicated assertion, guards still naming deleted namespaces, and maintained
  guidance that pointed at an unimportable `quickstart` module and a retired
  entrypoint.

### Added

- Add a repo-wide guard rejecting tests that pass without proving anything —
  empty parametrizations, self-comparisons, and same-scope duplicated
  assertions. Every shape it rejects was produced by this task's own mechanical
  test conversion and survived a green suite.
- Add regression coverage for the interrupt expiry path, whose error was
  unreachable before this release.
- Extend the canonical-imports guard to source docstrings and example scripts,
  which the AST scanner cannot see because they are prose, not imports.

## [0.17] - 2026-08-06

### Removed

- **Breaking:** delete the `zeroth.core` and `zeroth.econ_plane` import
  surfaces. Both trees, and every compatibility shim in them, are gone; the
  canonical eight-domain packages are the only import locations. A consumer on
  a legacy path must move to the canonical one — see
  `docs/backend-import-migration.md`.
- Remove `LEGACY_DOMAIN_PREFIXES` from the architecture policy. While it
  existed a dependency routed through a shim was scored as if it went straight
  to the canonical package, so several real cross-domain edges were invisible;
  they are now recorded as documented exceptions.
- Delete the compatibility suites whose only subject was the legacy paths. Each
  canonical assertion they carried — surface, cold-import, laziness — was moved
  into a canonical-only test rather than dropped.

### Changed

- Point CI's docstring gate at `src/zeroth` and set its threshold to the
  coverage that tree actually has. The old 92.3% was measured over a
  shim-dominated package; the denominator changed, not the bar.

### Documentation

- Rewrite the maintained documentation to canonical imports, and record in the
  migration guide that the legacy paths were removed in `0.17`. `CHANGELOG.md`
  and the migration guide keep naming the old paths on purpose -- they are the
  record a reader migrating off `0.16` needs.

### Fixed

- Bring `src/` into `ruff format` conformance. Eleven files predated the check
  and would have failed the release gate.

## [0.16.2.5.1] - 2026-08-07

### Added

- Report valid, fresh, signed `govern_graph` runs as `observed` through the
  gateway response header and terminal audit event.

### Fixed

- Keep exact signed run identity authoritative and close acquired upstream
  responses when capability reporting is cancelled.

## [0.16.2.3] - 2026-08-07

### Fixed

- Build the release image with the PostgreSQL, LangGraph adapter, and gateway
  dependencies exercised by its Compose smoke validation.
- Accept Syft's SPDX `DESCRIBES` relationship orientation while rejecting the
  reversed relationship in final release evidence.

## [0.16.2.2] - 2026-08-07

### Security

- Bind final release validation to the expected JUnit tests, installed image
  packages and labels, image-specific SPDX contents, and a cryptographically
  verified provenance receipt for the exported image before publication.
- Make final evidence validation the default and keep source-only validation an
  explicit pre-build phase.

## [0.16.2.1] - 2026-08-07

### Fixed

- Exercise the LangGraph release path through real local and loopback adapters,
  a durable public approval demo, and a runnable gateway fixture.
- Reject stale, incomplete, synthetic, or failed compatibility, performance,
  test, image, SBOM, and provenance evidence before publishing.
- Reject unsupported database backend values at settings validation time.

## [0.16.2] - 2026-08-07

### Added

- Ship reproducible LangGraph performance, compatibility, security, container,
  demo, and test evidence behind a fail-closed release checklist.
- Publish the canonical managed and self-hosted LangGraph deployment guide with
  explicit gateway-only enforcement limits.

## [0.16.1.7] - 2026-08-07

### Fixed

- Preserve exact compatibility fingerprinting and asynchronous WebSocket
  authentication across the canonical LangGraph gateway relocation.

## [0.16.1.6] - 2026-08-07

### Security

- Keep HTTP and WebSocket authentication responsive during remote JWKS reads,
  throttle key-refresh misses, and reject redirects outside HTTP(S).

## [0.16.1.5] - 2026-08-07

### Fixed

- Deliver async LangGraph policy audit records through the owning event loop
  while authorization remains offloaded.

## [0.16.1.4.1] - 2026-08-06

### Tests

- Assert edited approval arguments before execution and terminal compaction
  afterward in synchronous and asynchronous callable-wrapper coverage.

## [0.16.1.4] - 2026-08-06

### Fixed

- Keep async LangGraph tool graphs responsive while synchronous policy
  authorization waits, preserving fail-closed decisions and approval revalidation.

## [0.16.1.3] - 2026-08-06

### Security

- Remove original and edited tool arguments from terminal LangGraph approval
  records while preserving durable replay fences and exact retry validation.

## [0.16.1.2] - 2026-08-06

### Security

- Bound and cache remote JWKS retrieval, refresh rotated keys once, and reject
  unavailable or invalid key sets as bearer authentication failures.

## [0.16.1.1] - 2026-08-06

### Fixed

- Require an exact allowlisted Agent Server OpenAPI fingerprint before proxying
  and reject unavailable compatibility evidence.
- Compare recorded model, retry, disconnect, and graph outcomes through stable
  escaped paths, and pin the tested LangChain compatibility version in CI.

## [0.16.1] - 2026-08-06

### Added

- Run the pinned LangGraph and Agent Server compatibility pair in CI and report
  the exact first unexplained differential path without exposing compared values.

### Security

- Reject bearer tokens that omit expiry and verify replacement versus retired
  signing-key identifiers.

### Changed

- Relocate the LangGraph gateway out of `zeroth.core` into the canonical backend
  packages: data shapes to `zeroth.contracts.langgraph_gateway`, capability and
  event surfaces to `zeroth.governance.langgraph_gateway`, request-time machinery
  to `zeroth.service.langgraph_gateway`, and the adapter wire protocol to
  `zeroth.integrations.langgraph.enforcement_protocol`. Every legacy
  `zeroth.core.langgraph_gateway` import path keeps working through a lazy shim
  that resolves to the same objects.
- Invert the tool decision service's admission dependency onto a
  governance-owned `AdmissionEvaluator`, injected by the service domain, so
  governance no longer reaches into the service-classified gateway package.

### Removed

- Both temporary backend dependency exceptions for the LangGraph gateway. No
  exception key or edge references `langgraph_gateway` any more.

## [0.16.0.14.1] - 2026-08-06

### Fixed

- Integrate durable LangGraph approvals with the gateway architecture
  classification released concurrently on `main`.

## [0.16.0.14] - 2026-08-06

### Fixed

- Materialize frozen schema-less tool defaults before initial policy evaluation
  and execute the exact authorized call.

## [0.16.0.13] - 2026-08-06

### Fixed

- Fence an already-consumed LangGraph approval resume against identical
  reentrant execution so resumed tool calls execute at most once.
- Materialize schema-less tool defaults from the frozen executable before fresh
  approval policy evaluation and execute the exact validated call.

## [0.16.0.12] - 2026-08-05

### Fixed

- Match persisted LangGraph approval interrupts by exact schema and canonical
  JSON types before checkpoint confirmation.
- Rearm exact unconsumed terminal attempts that reuse an approval reference
  while retaining transition history and permanently fencing consumed work.
- Translate unavailable lifecycle storage into the typed durability failure.

## [0.16.0.11] - 2026-08-05

### Fixed

- Retain terminal LangGraph approval identity fences after rejection, resolved
  delivery, or uncertain consumed delivery while allowing pre-consume retries.
- Reject invalid persisted approval deadlines and leases during restart and
  reconciliation.

## [0.16.0.10] - 2026-08-05

### Fixed

- Preserve exact JSON scalar types across edited LangGraph approval execution
  and conflicting decision fences.
- Reject finite-number overflow before persisting approval or lease deadlines.

## [0.16.0.9] - 2026-08-05

### Fixed

- Reject non-finite LangGraph approval clock samples before persisting deadlines
  or reconciling durable work.

## [0.16.0.8] - 2026-08-05

### Fixed

- Bind each claimed LangGraph approval resume to its exact persisted action
  before policy evaluation or tool execution.
- Reject non-finite approval TTL and lease settings so reconciliation remains
  bounded.

## [0.16.0.7] - 2026-08-05

### Fixed

- Reject mismatched effective LangGraph checkpointers and ignore untrusted
  checkpoint coordinates while observing and resuming durable approvals.

## [0.16.0.6] - 2026-08-05

### Fixed

- Trust native LangGraph tool-call identities only from validated full call
  envelopes and refuse direct or unmatched injected identities.
- Resume approval-gated graphs from the latest thread state without retaining
  stale checkpoint IDs, namespaces, or checkpoint maps.
- Expire consumed approval leases by their effective deadline even when a
  bounded non-due request backlog precedes them.

## [0.16.0.5] - 2026-08-05

### Fixed

- Preserve each native LangGraph tool-call ID through governed sync and async
  execution so identical concurrent calls retain separate approval lifecycles.

## [0.16.0.4] - 2026-08-05

### Fixed

- Execute approval-edited plain callables from the same argument binding shown
  to fresh policy, including sync and async tools with variadic keyword inputs.
- Restrict latest-checkpoint resume to an internal identity capability so public
  graph configs preserve explicit checkpoint IDs and similarly named values.

## [0.16.0.3] - 2026-08-05

### Fixed

- Materialize plain-callable defaults before post-approval policy revalidation
  and execute the same canonical argument mapping on sync and async paths.
- Resume governed graphs from the latest thread checkpoint even when a stale
  checkpoint was bound earlier, and enforce one active SQLite approval identity
  across concurrent delivery and legacy-schema migration.

## [0.16.0.2] - 2026-08-05

### Fixed

- Consume persisted LangGraph approval decisions before replay policy checks,
  preserve approved edits and injected tool-call identities, and terminalize
  uncertain post-consume crashes without retrying their side effects.
- Resume parallel approvals serially from the thread's current checkpoint and
  add equivalent async checkpoint-confirmation and resume paths for async-only
  governed graphs and durable savers.

## [0.16.0.1] - 2026-08-05

### Fixed

- Harden LangGraph approval resume with durable-checkpointer attestation,
  interrupt-ID targeting, deadline and execution fencing, fresh authenticated
  governed-graph resume, native validation of edited tool arguments, and
  idempotent typed audit records.

## [0.16] - 2026-08-05

### Added

- Add a durable LangGraph approval lifecycle with checkpoint confirmation,
  idempotent decisions and resume claims, expiry and orphan handling, and
  restart-safe `Command(resume=...)` delivery to the original thread.

### Changed

- Require durable lifecycle storage for approval-gated tool calls, revalidate
  policy and edited arguments after resume, and fail closed before execution
  when thread or checkpoint semantics are unavailable.

## [0.15.1.0.1] - 2026-08-05

### Changed

- Classify `zeroth.core.langgraph_gateway` under the backend architecture
  policy through five legacy prefix mappings, so every module in the package
  resolves to a domain instead of escaping the dependency scanner.
- Record the two forbidden edges the classification exposes as documented
  temporary exceptions — the governance decision service's admission reuse and
  the LangGraph integration client's enforcement reuse — with removal tracked
  by the gateway relocation task.

## [0.15.1] - 2026-08-05

### Added

- Add the signed-context LangGraph decision boundary alongside the general
  governance enforcement API, including server-owned inventory matching,
  per-run attestations, approval identity, heartbeat evidence, and exact
  wrapper/middleware metadata parity.

### Fixed

- Bind decisions and attestations to the signed governance run identity,
  isolate concurrent idempotent writes, fail closed on metadata drift and
  resolver outages, and sanitize backend failures with exact-once metrics.

## [0.15.0.0.1] - 2026-08-04

### Fixed

- Regenerate the console API contracts (`frontend/openapi.json`,
  `frontend/app/lib/api-types.ts`) that had gone stale when the six
  `/v1/enforcement/*` routes landed; the docs workflow's drift gate caught the
  mismatch.

### Documentation

- Remove internal specification references from the changelog and the backend
  import-migration guide.

## [0.15] - 2026-07-31

### Added

- Provenance verification is now a seam of its own, separate from signing: `provenance.retired_keys_json`
  names the keys a deployment has rotated away from, and the verifier retains them so a record signed
  under a retired key stays verifiable. The verifier is built even when signing is disabled, because
  records signed earlier do not stop being the deployment's evidence.

### Fixed

- The run-attestation `409` no longer discloses another deployment's digest or expiry when a stored row
  had its payload and `digest` column rewritten together. Re-deriving the digest cannot detect a
  consistent rewrite; a deployment that holds verify keys now requires the stored signature to verify
  before any evidence is reported.

### Added

- LangGraph decision API and enforcement attestations: a versioned, tenant-scoped and idempotent
  policy decision boundary, plus the registration, signed run-start attestation, and heartbeat
  evidence needed to classify a run as `admission`, `observed`, or `enforced` truthfully.

## [0.13.14.8] - 2026-07-28

### Fixed

- Prevent governed plain callables from publishing an executable source through
  annotations, signature defaults, or schema carriers. Callable plans now sit behind opaque
  registry tokens, and unsafe metadata is refused rather than copied onto the public wrapper.
- Refuse a governed execution if a shared closure cell changes from ordinary state into an
  implementation after authorization, while preserving legitimate stateful closures.
- Execute an authorized frozen tool body directly instead of entering a second LangChain tool
  layer. Ambient configuration copies and process-global callback hooks can no longer run
  between the policy verdict and the body or rewrite the authorized arguments.

### Changed

- Emit one caller-visible outer callback tree per governed call. Tool-attached callbacks remain
  excluded, while genuine nested LangChain operations inherit the outer run context normally.
- Carry `response_format` and `handle_tool_error` on the governed outer wrapper so content and
  artifact shaping and `ToolException` handling happen exactly once.

## [0.13.14.7] - 2026-07-27

### Fixed

- Stop governed plain callables from publishing the original, ungoverned callable through
  `__wrapped__` or attributes copied by `functools.wraps`. The governed sync and async
  functions retain their name, documentation, module, qualified name, truthful signature,
  and exact LangChain-derived argument schema without exposing a direct policy bypass.
- Keep the existing fail-closed boundary for callables whose executable has no valid call
  signature. An over-bound `functools.partial` is still admitted at wrapping time, then
  refused before policy evaluation when invoked.

### Changed

- Remove the unused `ToolSnapshot.kind` field now that class-defined bodies are frozen and
  bound during snapshot capture. Update wrapper documentation to describe the
  framework-owned executing twin rather than the obsolete delegate invocation path.

## [0.13.14.6] - 2026-07-27

### Fixed

- Invoke the post-authorization executor with an explicitly callback-free config, on both the
  sync and the async path. An invocation with no config is not an invocation with no callbacks:
  `ensure_config` fills a missing one from `var_child_runnable_config`, the variable
  `BaseTool.run` republishes its own child config into, so the internal executor inherited every
  handler attached to the outer run and fired `on_tool_start` a second time — after the verdict,
  and before `_to_args_and_kwargs`. The mapping that hook is handed is a shallow filtered copy,
  so a policy that inspected `["safe", "evil"]` had the body run on `["safe", "evil", "evil"]`,
  the extra entry appended by a handler that had already, legitimately, run once before the
  decision. Dropping `callbacks` from the executing tool's carried fields closed the delegate's
  route to that hook and did nothing to this one. `{"callbacks": []}` rather than
  `{"callbacks": None}`: `ensure_config` merges the contextvar first and overlays an explicit
  config only for keys whose value is not `None`, so a `None` is filtered back out and the
  ambient handlers still win.

### Changed

- A caller's own run-level callbacks no longer fire twice per governed tool call. They see the
  governed tool's start and end exactly once, which is the tool they invoked; the internal
  executor beneath it is no longer a second traced run. No other ambient config key is
  overridden — `configurable` and the rest still reach a body through `get_config()`.
- **Tracing stops at the governed body, not only at the executor.** The executor's child config
  is what a body's own context carries, so anything the body goes on to invoke — a nested tool,
  a model, a `RunnableLambda` — is no longer seen by the caller's ambient handlers either. A
  body calling one inner tool used to produce three `on_tool_start` events for that handler and
  now produces one. Those handlers run after the body has already started and cannot change the
  authorized call, so this is a disclosed cost of the fix rather than part of it, and
  `test_what_a_governed_body_invokes_is_not_traced_by_the_callers_handler` pins it so that
  moving it is a decision.
- **Withdrawn as wrong, not superseded.** `_tool_execution`'s module docstring disclosed the
  handlers a caller attaches to their own run as acceptable, on the grounds that installing one
  is attacking yourself. Who installed a handler has no bearing on whether the body ran on the
  arguments the policy inspected, and that is the whole invariant; the paragraph has been
  replaced rather than amended.

## [0.13.14.5] - 2026-07-27

### Fixed

- Bind a governed call against the signature of the thing that runs, not against the label it
  carries. `inspect.signature` answers from `__signature__` when there is one and follows
  `__wrapped__` when there is one, and both are ordinary writable attributes on an ordinary
  admitted Python function — so a plain callable could describe itself as taking no parameters
  while its body went on materializing its own defaults. `remove(path="/danger")` with
  `__signature__ = "opaque"` was decided as `{}` and executed with `"/danger"`; a *valid*
  `inspect.Signature` naming fewer parameters did the same thing without raising anywhere, and
  a `functools.wraps` pointing at a narrower facade was a third way to say it. The binding is
  now taken from the frozen snapshot of the executable, which no longer carries either
  attribute, so the parameters a call is bound against are whatever the body's `__code__`,
  `__defaults__` and `__kwdefaults__` say they are.
- Drop `__signature__` and `__wrapped__` when freezing a function. The rebuild carries a
  function's `__dict__` across because a tool's own attributes are its state; these two are not
  state but *descriptions* of an implementation, and this module exists to stop a description
  and an implementation being two separately-readable things. The execution adapter already
  refused to write either of them for that reason.
- Delete the no-signature fallback on the plain-callable surface. It named arguments
  positionally and re-issued the caller's originals, on the stated grounds that only a builtin
  or a C function could reach it — and none can: every C-implemented shape is refused by
  `callable_implementation_digest` at `govern_tools` time. What actually reached it was an
  admitted Python function that had been told to have no signature. A call that will not bind
  is now refused as well, which loses nothing that could have executed and means no mapping the
  wrapper could not derive from the executable is put in front of a policy.

### Changed

- A `(*args, **kwargs)` decorator built with `functools.wraps` is now decided under the
  variadic parameters it really declares rather than under the inner function's parameter
  names. The argument is still in the mapping, one level down; a policy matching a decorated
  tool's inner parameter names must match `kwargs` instead.

## [0.13.14.4] - 2026-07-27

### Fixed

- Freeze what a governed body *captured*, not only the body itself. The snapshot rebuilt the
  outermost callable and then handed it the delegate's own containers, which left four ways to
  move what runs without moving anything the snapshot pointed at: a `functools.partial` was
  returned unchanged, so `partial.func.__code__` swapped the body underneath it; a callable
  object was returned unchanged, so `type(obj).__call__.__code__` did the same; a rebuilt
  function received the delegate's own `__closure__` tuple, so `cell.cell_contents = other`
  replaced the helper it called or the code object it executed; and `__kwdefaults__` was
  assigned across by reference, so one `__setitem__` rewrote a default the executing body
  materializes for itself. The snapshot now walks into every shape identity walks — it imports
  the fingerprint's own `_is_implementation` predicate and `_guard_depth` bound rather than
  restating either — rebuilding partials around a frozen `func` with copied argument
  containers, binding a statically resolved frozen `__call__` to the same instance, and giving
  a rebuilt function new cells for the implementation it closes over.
- Freezing stops at the line identity draws. A closure cell holding **state** stays the
  delegate's own cell, so a body that rebinds a counter through `nonlocal` still sees its own
  count on its next call; a `partial`'s bound arguments and a body's non-implementation
  defaults are carried across by reference; module globals stay shared; and a frozen `__call__`
  is re-bound to the same instance, so a tool that reads its own attributes goes on reading
  them. A `__call__` that is C-implemented is left alone and refused by the fingerprint a
  moment later, as it always was.

## [0.13.14.3] - 2026-07-27

### Fixed

- Decide a governed plain callable on the call it will actually run. The wrapper bound the
  arguments against the callable's own signature to *describe* the call, then re-passed the
  caller's original `args`/`kwargs` to *execute* it — two reads, with a parameter default
  materialized by only one of them. A callable `remove(path="/danger")` invoked with **no
  arguments** was therefore authorized as `{}` and executed with `"/danger"`: the policy
  approved an empty call and a destructive one ran. The binding now applies its defaults and
  is re-issued as `bound.args` / `bound.kwargs`, so every parameter arrives explicitly and
  the body's own `__defaults__` are never consulted a second time. A callable with no
  retrievable signature — a builtin, most C functions — still executes with the caller's
  arguments untouched, because there is no binding there to have described anything else.
  The empty `()` / `{}` entries `apply_defaults()` invents for `*args` / `**kwargs`
  parameters are suppressed, so no existing variadic tool's decided shape moves; a non-empty
  variadic keeps the nested shape it reports today.

### Removed

- Deleted the unused `_ENTRY_HOOKS` copy in `_tool_wrappers.py`. The authoritative table is
  the one `refuse_delegate_dispatch` reads in `_tool_execution.py`, and a second copy nothing
  imports is a table that can silently fall out of step with the one that enforces.

## [0.13.14.2] - 2026-07-27

### Fixed

- Stop carrying the delegate's `callbacks` onto the tool that executes after approval.
  `on_tool_start` fired between the decision and the body, and `BaseTool.run` hands that
  hook a **shallow** filtered copy of the tool input — so every container one level down
  is the same object the body is about to receive. A handler that appended to a list the
  policy had inspected as `["safe"]` had the body run on `["safe", "evil"]`, with nothing
  reassigned and nothing visible on the tool. Governed execution therefore does not run a
  delegate tool's own callbacks at all; observability comes from the governance audit
  trail, not from a hook that runs between the verdict and the call. `response_format` is
  still carried, so a `content_and_artifact` tool keeps its artifact.
- Install each snapshotted body behind an internal `(*args, **kwargs)` adapter, so
  `StructuredTool` has nowhere to inject arguments no policy inspected. Both `_run` and
  `_arun` read the *body's own* signature and add a live child callback manager under a
  declared `callbacks` parameter and the whole run configuration under a
  `RunnableConfig`-annotated one. The adapter carries neither that parameter name nor any
  annotation — and deliberately no `functools.wraps`, `__wrapped__` or `__signature__`,
  each of which would hand the framework the body's real signature straight back. Proved
  on all four driver combinations — `govern_tools` and `ZerothMiddleware`, sync and async
  — across the `func` field, a `functools.partial` field and a hand-written `_run`.

## [0.13.14.1] - 2026-07-27

### Fixed

- Freeze every captured tool body **by value**, and bind a class-defined one **before**
  any caller-supplied code runs. The snapshot that closed the moved-field vector still
  stored the delegate's own callables, so `body.__code__ = other` swapped what executed
  while the field, the signature and the snapshot itself all stayed put — half-covered by
  chance on the plain-callable surface, which rebuilt functions, and not covered at all
  on the `BaseTool` surface, which froze nothing. Every captured slot is now rebuilt
  through `snapshot_callable`, bound methods included, so the wrapper holds code nothing
  else has a reference to.
- Move method binding out of execution and into the snapshot. A `_run` used to be bound
  by calling the delegate's own `__get__` *after* the side-effect classifier, the
  contract resolver and the decision client had run, and a descriptor that is
  fingerprinted through `__call__` and invoked through `__get__` could answer the two
  reads with two different bodies. Only a plain function, a `staticmethod` and a
  `classmethod` are admitted — what a hand-written tool actually declares — and any other
  descriptor is refused with `UnstableToolIdentityError` rather than bound by its own
  code. `langchain_core`'s own `_run`/`_arun` boilerplate is still skipped rather than
  captured, so an ordinary tool never reaches that refusal. `ToolSnapshot.delegate` is
  gone with its only reader: no delegate-written code runs after authorization to produce
  the body. Both vectors are proved on all four driver combinations — `govern_tools` and
  `ZerothMiddleware`, sync and async.

## [0.13.14] - 2026-07-27

### Fixed

- Establish, and prove, that **the body which runs is the body whose identity was
  authorized, and the arguments it receives are the arguments the policy saw**. Identity
  was previously derived from the live tool and the body fetched from it again at
  execution time, with the caller's contract resolver, side-effect classifier and
  decision client running in between — so anything that moved the tool in that window
  executed under a fingerprint pinned before it moved, and every read governance made
  could be answered by the delegate itself. A `BaseTool` overriding `model_copy` returned
  an evil same-schema `StructuredTool` that ran in place of the authorized one; a
  classifier replacing `target.func` when consulted ran a substituted body under the
  unchanged pin; an `invoke` entry in the delegate's instance `__dict__` and a custom
  `__getattribute__` both bypassed a refusal that read only class-level attributes. All
  four reproduced on both enforcement surfaces, sync and async.

### Added

- `zeroth.integrations.langgraph._tool_execution`, which makes identity derivation and
  execution one fact instead of two reads. Reads are **static** — the real `__mro__`
  walked through `type`'s own descriptors, instance state through
  `object.__getattribute__` — so neither a hostile `__getattribute__` nor a metaclass can
  author them. A **per-call snapshot** captures body and surface by value before any
  caller-supplied code runs, identity is digested from that snapshot
  (`tool_slots_digest`), and execution runs a **framework-constructed** `StructuredTool`
  carrying the snapshotted callables rather than a copy the delegate produced — an object
  the delegate has no attribute on.

### Changed

- The entry-hook refusal is widened to `model_copy`, `__copy__`, `__deepcopy__`,
  `__getattribute__` and per-instance shadows of every hook, plus a metaclass allowlist,
  checked at wrap time and again per call. `__getattr__` is permitted by identity rather
  than refused, because `BaseModel` defines it. This refusal is deliberately **defence in
  depth and not the guarantee**: a list of banned attributes is a list the next probe
  walks around, which is why the snapshot carries the invariant.
- A delegate whose entry path governance cannot execute past is now refused **before** the
  policy is consulted rather than after. That is the ordering every other fail-closed
  refusal already used, and it stops a live decision client from spending an answer on a
  call that can never execute.
- A governed plain callable now executes a function rebuilt from its own parts, so a later
  `__code__` reassignment cannot move the body the wrapper holds.

## [0.13.13.3] - 2026-07-27

### Documentation

- Document the exact boundary of the injected-argument refusal, and record the
  decision not to project injected arguments away. An injected argument is
  projected by the same canonical rule as every other argument, so its **value**
  decides the outcome: the whole graph state (`Annotated[dict, InjectedState]`) and
  an `InjectedStore` handle are refused, while a narrowed
  `InjectedState("user_id")` and an `InjectedToolCallId` are representable and are
  governed normally. The cookbook now states the workaround — narrow the injection
  to the slice the body needs, which is also the declaration a policy can be
  written against — and why eliding the value is not an option: a policy that
  denies `path="/etc/shadow"` must not be shown a call with the path removed. This
  narrows the `0.13.13` note below, which claimed the refusal covered
  `InjectedState` / `InjectedStore` as such.

### Tests

- Cover all four injected-argument shapes in the cross-surface parity table,
  driven through a real `create_agent` invocation on both surfaces — the argument
  table above hands tools their arguments directly, so it could not observe
  injection at all. Confirms that `govern_tools` and `ZerothMiddleware` refuse and
  allow the same shapes, and that the two representable shapes really are governed
  rather than merely tolerated.
- Extract the scripted tool-calling model into
  `tests/integrations/langgraph/tools/_agents.py`, so the middleware suite and the
  parity table drive one definition instead of two copies.

## [0.13.13.2] - 2026-07-26

### Documentation

- Document that tool identity covers implementation code and the declared surface,
  not instance configuration: two instances of the same configurable `BaseTool`
  subclass — one pointed at a benign endpoint, one at a hostile one — carry the
  same fingerprint, so a reconfigured instance is not a detected substitution. The
  trade-off is forced, because identity is re-derived and compared on every call
  and digesting mutable bound state would make a stateful tool refuse its own
  second call. Records what to do instead: pass the configuration as a tool
  argument, pin it in `contract_ref`, or give each configuration its own
  implementation.

## [0.13.13.1] - 2026-07-26

### Fixed

- Pin the async half of the un-substitution limitation: a middleware nested inside
  `ZerothMiddleware` can strip the governed twin from `awrap_tool_call` exactly as
  it can from `wrap_tool_call`, leaving no decision and no audit record. Both are
  now asserted against a pass-through control, so neither test can go vacuous.

## [0.13.13] - 2026-07-26

### Fixed

- `ZerothMiddleware` no longer authorizes the raw arguments the model emitted. It
  now substitutes a per-call governed twin of `request.tool` into the request it
  hands downstream, so `BaseTool` validation, coercion, defaulting and `ToolNode`
  argument injection all happen **before** the shared guard runs. Policy
  previously saw the string `"7"` while the body received the integer `7`, and
  never saw a defaulted or injected argument at all.
- The middleware carries no enforcement branch of its own any more: its calls to
  `guard_tool_call` / `authorize_tool_call` are removed, so both install surfaces
  reach the enforcement core at one point (R8).
- A tool that overrides a pre-body entry point (`_parse_input`, `invoke`, ...) is
  now refused on the middleware surface too, as it already was on `govern_tools`.
- Passing an already-governed tool to `ZerothMiddleware` is now refused with
  `UnstableToolIdentityError` instead of being decided and recorded twice.
- A retry middleware nested *inside* governance now gets a decision and an audit
  record per physical execution, retiring a previously documented limitation.

### Changed

- **Breaking for mis-installed chains.** The verdict now travels back up *through*
  the LangChain `handler`: a denial raises, and an approval interrupts, inside the
  tool's execution rather than before the handler is reached. A middleware nested
  inside `ZerothMiddleware` therefore observes calls governance goes on to refuse,
  and one that rewrites `request.tool` removes governance entirely. Install
  `ZerothMiddleware` last.
- A tool whose injected argument carries an unrepresentable value — the whole graph
  state, an `InjectedStore` handle — is now refused on the middleware surface,
  because the injected value reaches the canonical argument projection. This
  matches what `govern_tools` already did. A narrowed injection whose value is
  representable, such as `InjectedState("user_id")` or `InjectedToolCallId`, is
  governed normally; see the cookbook for the boundary.

## [0.13.12] - 2026-07-26

### Fixed

- **A governed tool executed a second, mutable reference to its delegate.**
  `GovernedTool` carried `zeroth_delegate` and `zeroth_plan` as public,
  assignable pydantic fields while re-deriving the *identity* it enforces from
  the plan — so assigning a same-schema tool over `zeroth_delegate` after
  `govern_tools` returned made that tool execute under the original's authorized
  fingerprint. A confused deputy, reproduced by probe. The plan is now sealed in
  private state, execution goes through `plan.target`, `__setattr__` refuses
  every name a call is executed or reported through, and the callable surface
  publishes no handle to its target or plan at all.
- **A delegate overriding a pre-body entry point could run arguments policy never
  saw.** Clearing `args_schema` on the per-call executing twin makes `BaseTool`'s
  `_parse_input` a pass-through, but does nothing about a subclass that
  *overrides* it: policy authorized `{"query": "safe"}` while the body received
  `"danger"`. **Breaking, deliberately:** a delegate whose class overrides
  `_parse_input`, `_to_args_and_kwargs`, `invoke`, `ainvoke`, `run` or `arun`
  is now refused with `UnstableToolIdentityError`, at `govern_tools` and again
  before every execution. `langchain-core`'s own `BaseTool` and `StructuredTool`
  implementations are permitted, so the `@tool` decorator's output — the normal
  case — is unaffected; hand-written subclasses that override one of those six
  hooks, `langchain_core.tools.Tool`, and re-governing an already-governed tool
  are not. See the cookbook's "What `govern_tools` refuses to wrap". The ban can
  be lifted once a neutral execution adapter exists that bypasses those hooks.
- **Tool identity ignored exception handling.** The code projection omitted
  `co_exceptiontable`. Since Python 3.11 exception handling is zero-cost —
  entering a `try` emits no opcode — so two bodies with byte-identical
  `co_code`, constants and names can differ in whether a `ZeroDivisionError` is
  caught or propagates, and they fingerprinted identically. The table is now part
  of the projection. Every other `co_*` attribute is documented as projected or
  excluded, with the reason, in `_tool_fingerprint`'s module docstring.
- **Recording a tool inventory consumed live authorization resolvers.** Both
  install surfaces asked the caller's `side_effect` classifier and `contract_ref`
  resolver for a reading at construction. A resolver is allowed to be live, and
  asking one *consumes* an answer — so every later call was decided under the
  following answer. On `ZerothMiddleware` this meant the same installation denied
  a call **without** `expected_tools` and allowed it **with** it; on
  `govern_tools` the first real call was decided under the classification meant
  for the second. Neither surface resolves anything while recording now: the
  inventory carries the identity, `SideEffectClass.UNKNOWN` and no contract, and
  both facts are resolved live on every call as before. A caller that wants a
  classified inventory builds `GovernedToolBinding` values from its own
  observations and passes them to `record_binding_inventory`.

## [0.13.11] - 2026-07-26

### Added

- **The LangGraph tool-governance vocabulary is now public API.** The
  enforcement path gates on *exact type* — a verdict counts only when it is
  exactly a `ToolDecision` carrying exactly a `ToolDecisionKind` — so the types
  needed to call the enforcement surfaces were mandatory, not optional
  implementation details, yet only `govern_tools` / `GovernedTool` /
  `ZerothMiddleware` were exported. A user could not construct an *allowing*
  decision client through supported imports at all. `zeroth.integrations.langgraph`
  now exports the vocabulary a caller provably needs: `ToolGovernanceContext`,
  `ToolIdentity`, `ToolAction`, `ToolDecision`, `ToolDecisionKind`,
  `SideEffectClass`, `ToolDecisionClient`, `FailClosedToolDecisionClient`,
  `UnknownSideEffectPolicy`, `ToolAuditSubmitter`, the five typed refusals
  (`ToolGovernanceError`, `PolicyViolation`, `GovernanceContextError`,
  `UnstableToolIdentityError`, `ApprovalRequiresThreadError`) and the inventory
  reporting surface (`ToolInventory`, `ToolInventoryEntry`, `InventoryCoverage`,
  `ToolInventoryMatch`, `ToolEnforcementReport`, `record_tool_inventory`,
  `report_tool_enforcement`, `match_tool_inventory`,
  `attest_complete_inventory`). The normalizers, the fingerprint digests and the
  enforcement entry points stay private: normalization is done *for* a client
  before it is asked, identities are derived rather than caller-asserted, and
  enforcement lives in exactly one place. All of it is imported **eagerly** —
  none of it touches `langchain` at module scope — so the guarantee that
  importing the package pulls in neither `langgraph` nor `langchain` is
  unchanged.
- **`zeroth-core[langgraph]` extra.** `langchain` and `langgraph` existed only in
  the `gateway-conformance` *dependency group*, so the documented middleware
  capability was not installable from the published package. They are now a real
  project extra (`pip install "zeroth-core[langgraph]"`), ranged rather than
  pinned to the Agent Server conformance versions, and deliberately not part of
  `[all]` (the headless runtime bundle). The imports stay lazy: the extra is how
  a user opts in, not a licence to import eagerly.

### Fixed

- **The default-interrupt test could not fail.** It only inspected
  `_langgraph_interrupt.__code__.co_names`, so it passed even if the approval
  path never selected that function. The approval branch now patches the module
  seam, invokes the guard with no `interrupt` argument, and asserts the
  substitute received the real payload before a zero-count downstream; the
  bytecode check survives as a separate, genuinely different claim (that the
  default *is* LangGraph's `interrupt`). Both stay Tier B — making the seam
  injectable is precisely what freed the approval branch from needing
  `langgraph`.
- **The cookbook misstated which tier the compatibility matrix runs in.** It
  said all matrix tests were conformance-tier; the eight `test_cell_*` wrapper
  cases deliberately run in the default (base) tier, and only the cross-surface
  parity table is Tier A. Operators following the page ran the wrong suite. The
  recipe also now uses only public imports and documents the install extra.

## [0.13.10] - 2026-07-26

### Fixed

- **Tool identity was too weak to detect substitution.** A governed tool's
  fingerprint covered only its *surface* — name, description and argument
  *names* — so two functions returning different results, bound under one name
  with one description and one schema, received the same identity. A substituted
  tool therefore passed inventory matching and inherited the authorization
  granted to the original. Identity is now bound to the code the tool will
  actually run (`_tool_fingerprint.py`): a SHA-256 digest over the callable's
  own code object — bytecode, constants, names and parameter shape, recursing
  through closures, bound methods, `functools.partial` and decorators — plus a
  digest of the *complete* declared schema, field types and constraints
  included, rather than field names. Derived rather than caller-asserted,
  because a fingerprint the caller supplies is a claim the substituting party is
  best placed to make. A tool whose implementation cannot be fingerprinted
  stably — a builtin, a C extension function, a tool whose body slot raises
  rather than answering — now raises `UnstableToolIdentityError` instead of
  falling back to the weak surface identity.
- **The substitution test proved the wrong thing.** The R11
  "same-name-different-fingerprint" case varied the tool's *description*, which
  is metadata a real substitution simply copies. It now varies the
  implementation with name, description and declared schema held identical and
  asserted identical first.

## [0.13.9] - 2026-07-26

### Fixed

- **"Exactly once" was asserted on the handler, not on the tool body.**
  `ZerothMiddleware.wrap_tool_call` calls its downstream once per decision, and
  every test counted that downstream — the same layer the assertion was about.
  But LangChain hands each middleware a handler its own body may call repeatedly
  (`_chain_tool_call_wrappers.compose_two`: "Outer can call call_inner multiple
  times"), so a retrying middleware declared *after* `ZerothMiddleware` nests
  inside it and runs the tool body N times against one decision and one audit
  record. `ZerothMiddleware` is now documented as a hard **install-last**
  requirement: innermost, every attempt re-enters `wrap_tool_call`, so every
  physical tool execution gets its own decision and its own audit record. The
  contract is pinned by tests that count executions of the tool function itself
  — below `ToolNode` and below every middleware — driven through a real
  `create_agent` with a real retrying middleware, sync and async. The
  nested-retry limitation is pinned by its own test so it can never silently
  become a claim, and no unqualified "exactly once" about the *body* remains in
  the docs or docstrings. No position check is attempted: `AgentMiddleware`
  exposes no hook carrying the middleware list, and the only observable
  difference between the two arrangements is whether the handler is the tool
  executor or LangChain's private `compose_two.<locals>.call_inner` — a guard
  built on another library's local closures would stop guarding silently.
- **The middleware had no inventory and no enforcement report.** The R13 report
  was only ever exercised through `record_tool_inventory(govern_tools(...))` —
  the wrapper path — which proves nothing about a middleware-only install, where
  no tool is wrapped and no `zeroth_binding` exists to read.
  `ZerothMiddleware(expected_tools=...)` now records a declared inventory,
  exposed as `tool_inventory` and reported by `enforcement_report()`. The tools
  are **not injected**: they are not added to `middleware.tools`, not handed to
  the agent, and not wrapped. They are pinned through the same
  `_describe_base_tool` a live call is described through, so an entry carries the
  fingerprint the decision is actually made under. The report goes through the
  one `report_tool_enforcement` the wrapper surface uses, so it can never be
  `enforced` and mints no capability evidence: a tool-only install reports
  `observed` (or `admission` when nothing was declared) with `partial` coverage
  and an explicit enforced-tool list. The inventory gates nothing — an
  undeclared tool is decided exactly as any other call is.
- **The optional-dependency guard named the wrong module.** The middleware and
  surface-parity suites called `importorskip("langgraph")` and then imported
  `langchain.agents`. Marked modules are imported at collection, so an
  environment with `langgraph` but without `langchain` errored instead of
  skipping. Both suites now guard `langchain.agents` directly, which covers both
  dependencies.

## [0.13.8] - 2026-07-26

### Fixed

- **Tool enforcement authorized arguments the tool body did not receive.**
  `GovernedTool._run` / `._arun` built the policy action from the arguments
  `BaseTool` had already parsed and then handed them back through
  `delegate.invoke`, which validated them a *second* time. A validator that is
  stateful or otherwise non-idempotent answers differently on the second pass,
  so policy authorized one value while the body executed another. The delegate
  is now driven through a per-call pass-through twin
  (`_tool_wrappers._executing_delegate`), so exactly one validation happens per
  governed call and the values the body receives are the values the decision was
  made about. The delegate is still driven through its own `invoke`, so it keeps
  building its own `ToolMessage` and a `content_and_artifact` tool's artifact
  still survives the wrapping. The original tool is not mutated: the
  pass-through is arranged on a copy.
- **The two install surfaces resolved authorization facts at different times.**
  `govern_tools` pinned both the side-effect classification and the contract
  binding at wrap time, while `ZerothMiddleware` resolved both per call, so a
  tool that became `SIDE_EFFECTING` (or moved onto another contract) after it
  was wrapped was still decided as what it used to be on the wrapper surface —
  and the stale reading is always the permissive one. Both surfaces now resolve
  every authorization fact per call. `GovernedToolBinding.side_effect` /
  `.contract_ref` survive as the inventory's wrap-time *observation* and nothing
  decides against them.
- **The unclassified gate was not exact-type safe.** `_denies_unclassified`
  treated every value other than the `UNKNOWN` member as classified. Because
  `SideEffectClass` is a `StrEnum`, an exactly-typed `ToolAction` carrying the
  bare string `"read_only"`, `None`, or any other non-member reached the
  decision client as a *classified* tool. Anything that is not an actual enum
  member is now unknown, and unknown is denied unless the named opt-in says
  otherwise.
- **Non-finite floats broke the canonical-JSON promise.** `NaN` and the
  infinities passed the argument projection although `json.dumps` spells them
  `NaN` / `Infinity`, which no other parser reads back — so the argument
  fingerprint and every audit record built from it stopped round-tripping, and
  `NaN` compares unequal to itself, which no policy can decide by comparison.
  They are now refused like every other unrepresentable value.

### Added

- Cross-surface parity scenarios for stateful resolvers:
  `a-classification-that-changes-after-the-tool-is-installed` and
  `a-contract-binding-that-changes-after-the-tool-is-installed`. The table
  previously avoided stateful resolvers by design, and that exclusion is what
  hid the resolution-timing divergence; `Scenario` now carries `resolvers` /
  `mutate` hooks so a fact can be changed in the window between installing a
  tool and calling it.

## [0.13.7] - 2026-07-26

### Added

- `tests/integrations/langgraph/tools/test_surface_parity.py`: a cross-surface
  parity suite built on one shared scenario table. Every scenario is driven
  through `govern_tools` **and** `ZerothMiddleware` inside the same test body,
  and the first assertion is that the two outcomes are equal — exception class,
  returned value, downstream call count, every audit projection, and every
  interrupt payload. The table covers allow, deny, require-approval,
  require-approval-without-a-thread, unknown-side-effect denied, unknown
  side-effect admitted by the named opt-in, absent governance context, and a
  decision client that raises. Sync and async halves of both surfaces are held
  to the same equality, and one surface's own sync/async pair is compared too,
  so the four edges of the square are pinned rather than three.
- `docs/how-to/cookbook/govern-langgraph-tools.md`: the tool enforcement
  documentation page, nested under the existing How-to Guides / Cookbook nav
  section. It carries the `govern_graph(create_agent(..., middleware=[...]))`
  composition example, the supported middleware nesting order
  (first-defined-outermost), the compatibility matrix with each documented cell
  mapped to its named test, and the two known divergences.

### Documented

- Tool-level enforcement is **not** graph-level or cumulative enforcement.
  `ENFORCED` requires capability evidence that is both `ENFORCED` and
  `tool_manifest_complete`; nothing in `src/` mints evidence with
  `tool_manifest_complete=True`, and this integration states in four places that
  it never promotes a run above `admission` (FA5). A tool-only run therefore
  reports `observed` with `partial` coverage plus an explicit enforced-tool list.
- `response_format` is deliberately not carried onto a governed wrapper: an
  attribute divergence with behavioral equivalence, because the delegate is
  handed the whole tool call and builds its own `ToolMessage`. Carried fields are
  `return_direct`, `tags`, `metadata`, `handle_validation_error`; not carried are
  `callbacks`, `handle_tool_error`, `response_format` — split by who reads them.
- Two callback trees fire per governed call: the wrapper's `run()` and the
  delegate's `run()` each emit `on_tool_start` / `on_tool_end`. Telemetry
  divergence, not a governance gap — the body still runs once and the decision is
  still recorded once.

### Fixed

- The parity suite's first run surfaced `ApprovalActionRecord.occurred_at` as a
  nested per-record wall clock, so the audit projection excludes it alongside
  `audit_id` and `started_at`. Those three — two timestamps and a nonce — are the
  only excluded fields; every other field is compared.

## [0.13.6] - 2026-07-26

### Added

- `_middleware.py`: `ZerothMiddleware`, the `create_agent` install surface for
  tool enforcement. It implements both `wrap_tool_call` and `awrap_tool_call`,
  and carries no enforcement branch of its own — the sync path calls
  `guard_tool_call`, the async path calls `authorize_tool_call` and then awaits
  its own downstream, exactly as the `govern_tools` wrappers do. LangChain hands
  the tool's execution in as a `handler` callback, so a denial and an approval
  are literally "the handler was never called" rather than a verdict inferred
  from a return value.
- A denial propagates as `PolicyViolation` and is never rendered as an error
  `ToolMessage`. Governance decides whether a call happens, not what it means; a
  second representation of the verdict is exactly the drift that lets two
  surfaces disagree.
- Identity is read off the *resolved* tool through the same `_describe_base_tool`
  the wrapper surface uses, so one tool fingerprints identically on both install
  surfaces, and a request naming one tool while carrying another is refused. A
  call the agent could not resolve to a tool (`request.tool is None`) is refused
  rather than decided against a bare name.
- The supported nesting order is documented on the class: LangChain composes
  `wrap_tool_call` middleware first-defined-outermost, so governance sees every
  call the middleware ahead of it let through, and everything nested inside it
  only ever runs on a call governance allowed.
- `ZerothMiddleware` is exported lazily from `zeroth.integrations.langgraph`, for
  two independent reasons: it needs `langchain.agents`, which ships only in the
  optional `gateway-conformance` group, and importing that drags in the
  OpenTelemetry SDK by way of `langsmith`.

### Fixed

- The eager-import guard now forbids `langchain` as well as `langgraph`. It
  previously checked only the latter, so a module-scope `langchain.agents` import
  in the package's `__init__.py` would have sailed past it and instead
  hard-`ImportError`ed the package for every caller installed without the
  optional group. The match is `== "langchain"` / `startswith("langchain.")`:
  `langchain_core` is a *core* dependency imported eagerly on purpose, and a bare
  `startswith("langchain")` would fail on the 54 modules it already pulls in — a
  second test pins that distinction rather than leaving it to a comment.
- `test_tool_inventory.py`: replaced a vacuous assertion in
  `test_the_module_mints_no_capability_evidence`. It checked that
  `"tool_manifest_complete"` was not a *key* in the module namespace, which no
  module-level binding could ever be; it now checks the namespace's values, which
  is where such a claim could actually be minted.

## [0.13.5] - 2026-07-26

### Added

- `_tool_inventory.py`: records the governed tool inventory from what
  `govern_tools` pinned, and reports the enforcement level that honestly
  supports. `record_tool_inventory` reads each wrapper's binding structurally —
  no `langchain_core` import, so the module pulls in neither `langsmith` nor the
  OpenTelemetry SDK — and refuses a tool that carries no governed binding rather
  than skipping it, because a tool that cannot be read must not become a tool
  that cannot be seen.
- `match_tool_inventory` compares an inventory against the tool list an operator
  declared on name **and** fingerprint, order-insensitively, and reports the three
  ways it can differ as three separate findings: a missing tool, an unexpected
  tool, and a *substituted* one — same name, different fingerprint. The third is
  the direction a name-keyed inventory waves through, so it is kept apart from
  the other two rather than folded into them. `inventory_fingerprint` digests the
  sorted name/fingerprint pairs through the existing canonical projection, so it
  inherits that projection's exact-type refusals.
- A repeated tool name is refused on both sides: a name two tools answer to
  identifies neither, and makes "extra or substitution?" ambiguous.
- `attest_complete_inventory` is the only place `InventoryCoverage.COMPLETE` is
  minted, and it mints it only against a declared list that matched exactly.
- `report_tool_enforcement` returns `observed` with the names of the tools
  actually governed, and `admission` when there are none. It has no branch that
  can return `enforced`: that needs signed, fresh, `tool_manifest_complete` run
  evidence, this package mints none, and an inventory's coverage is not a
  substitute for it — an attested `COMPLETE` inventory reports exactly what a
  partial one does. The report is a description of the tool surface, not a run
  attestation, and is not fed to a `CapabilityReporter`.
- `ToolEnforcementReport.level_term` is the plain `str` an audit writer must use:
  `execution_metadata["governance_level"]` is vocabulary-gated with
  `type(value) is str`, and a `StrEnum` member's type is the enum class, so
  writing the member itself passes the membership test and is then summarized
  away at capture time.
- Every identity is rebuilt from fields that passed `normalize_identifier`, on
  the recorded side and the declared side alike, so a `str` subclass smuggled
  into a caller-constructed `ToolIdentity` cannot forge a match, and a hostile
  container that injects a value through `__iter__` refuses the recording.

## [0.13.4] - 2026-07-26

### Added

- `_tool_wrappers.py`: `govern_tools`, the install surface for a raw tool list.
  It returns governed twins of sync and async `BaseTool` instances and of plain
  callables, each invocable through exactly the interfaces its original was —
  `invoke` / `ainvoke` / `run` / `arun` for a tool, a direct call for a callable —
  and each preserving `name`, `description` and `args_schema`. A wrapper also
  reports the wrapped tool's own input schema rather than one inferred from the
  wrapper's signature, so a schema-less tool stays schema-less through the
  wrapping. `return_direct`, `tags`, `metadata` and `handle_validation_error` are
  carried too — the caller reads those off the wrapper and never off the delegate
  behind it — while `callbacks`, `handle_tool_error` and `response_format` stay
  with the delegate, which is the layer that runs, formats and therefore fails.
- A governed call that arrived as a tool call is handed on as a tool call, id
  included, so a `content_and_artifact` tool builds its own `ToolMessage` and its
  artifact survives the wrapping instead of being dropped for want of a call id.
- The wrappers are adapters, not a second enforcement path: the sync surfaces
  call `guard_tool_call` and the async surfaces call `authorize_tool_call` and
  then await their own downstream, so every allow, deny and approval branch stays
  in the one shared core. `_run` / `_arun` are the choke point, which is what
  makes every `BaseTool` entry point governed at once.
- Wrapping mutates nothing: the original tool object is never written to, its
  `.func` / `.coroutine` are never rebound, and the supplied container is copied
  rather than governed in place, so any other holder of a wrapped tool keeps the
  behaviour it had.
- Identity is pinned at wrap time and re-derived from the live tool on every
  call; a name or declared schema that moved refuses the call with
  `UnstableToolIdentityError` rather than deciding against an identity that will
  not hold.
- Optional per-tool `side_effect` and `contract_ref` resolvers. Only a real
  `SideEffectClass` member classifies a tool, and a resolver that raises leaves
  the tool unclassified — which the default policy denies.
- `govern_tools` declares `partial` coverage and takes no coverage parameter:
  claiming a complete inventory needs an expected tool list whose fingerprints
  match at startup, and a parameter would let a caller assert completeness
  nothing verified.

### Changed

- `govern_tools` / `GovernedTool` are exported from
  `zeroth.integrations.langgraph` **lazily**, alongside `emit_genai_spans`:
  `from langchain_core.tools import BaseTool` pulls `langsmith`, which imports
  the OpenTelemetry SDK at module scope, so an eager export would drag
  OpenTelemetry into every `import zeroth.integrations.langgraph`.

## [0.13.3] - 2026-07-26

### Added

- `_tool_guard.py`: the shared tool-enforcement core both governance surfaces
  call, so the fail-closed rules cannot diverge between them. `guard_tool_call`
  is the entry point — it invokes the downstream tool exactly once on an allow,
  outside any `try` and any loop, so a tool's own failure propagates unchanged
  and is never retried. A denial raises `PolicyViolation` and an approval
  suspends the run, both *before* the tool body, which runs zero times either
  way. `authorize_tool_call` is the same enforcement without the invocation, for
  a surface whose downstream call is awaited.
- LangGraph's `interrupt` is an injected parameter defaulting to a lazy import,
  the same seam shape as the decision client: an import inside the enforcement
  function would leave nothing for a test to substitute, so the approval branch
  would be untestable without the dependency. The interrupt payload is versioned
  and carries only plain `str`/`int`/`None` values, so it survives a checkpoint's
  JSON round trip; the call's arguments are represented by their fingerprint
  rather than their content. Requesting an approval is all this stage does —
  resume-time revalidation is ZER-10's, and an `interrupt` that returns instead
  of suspending is refused rather than followed by the invocation.
- Two further fail-closed rules: a tool with no stable identity raises
  `UnstableToolIdentityError`, and an approval on a run with no thread to resume
  into raises `ApprovalRequiresThreadError` rather than quietly becoming an
  allow. Both refusals happen before anything is recorded or interrupted.
- An action normalized against one governance context can no longer be enforced
  against another: the principal is checked and a mismatch raises
  `GovernanceContextError`. Not attacker-reachable, but it is attribution an
  auditor could not unpick afterwards.

### Changed

- The decision record travels the typed `tool_calls` and `approval_actions`
  fields, never a new `execution_metadata` key: the metadata allowlist has no key
  for a tool name, tool reference or approval id, and drops unrecognized keys
  silently, so writing one produces a record that looks audited and carries no
  evidence. Only `decision` and `reason_code` — both long-standing allowlisted
  keys — go into the metadata, and `reason_code` only for a denial. Emission
  never raises: a projection that will not validate and a submitter that fails
  each cost one record, not the call.
- `capture_vocabulary`: added `require_approval` to
  `METADATA_VOCABULARIES["decision"]`. `allow` and `deny` were already retained,
  and the existing `approve`/`reject` are `ApprovalDecisionType` *resolutions* —
  what a human answered — so neither could stand in for "waiting on a human".
  Without a term of its own, the one verdict that pauses a run is the one whose
  record loses it.

## [0.13.2] - 2026-07-26

### Added

- `_tool_normalize.py`: builds the complete `ToolAction` a tool-governance
  decision is made about *before* any policy runs — tool identity, canonical
  argument projection with a stable SHA-256 fingerprint, contract binding,
  injected principal, side-effect classification. Every gate is an exact-type
  gate (`type(x) is str`), every container is copied before it is validated,
  and mappings are read by iterating `items()` rather than keying back into
  them, so a hostile `str` / `dict` / `tuple` / `list` subclass cannot reach
  the descriptor. An argument the projection cannot represent is refused, never
  elided or replaced: a placeholder would show a policy a different call from
  the one about to run.
- `_tool_decisions.py`: the decision seam — a `ToolDecisionClient` protocol and
  `FailClosedToolDecisionClient`, the default that denies every call.
  `resolve_tool_decision` turns a missing client, a raising client, a `None`, a
  duck-typed impostor and an unrecognized verdict all into denials; an
  unclassified (`SideEffectClass.UNKNOWN`) tool is denied before a client is
  even asked. The only escape is `UnknownSideEffectPolicy.ALLOW_UNCLASSIFIED_TOOLS`,
  an enum member compared by identity so no boolean, default or bare string can
  trip it. Every reason code the seam mints is registered in `REASON_CODES`,
  and repairing an unregistered client-supplied code never changes the verdict.
- The principal and tenant are injected, never read from the unverified
  correlation carrier: a test pins that a forged correlation id cannot become a
  principal, structurally (no import edge) and behaviourally.

### Changed

- Two tool reason-code assertions that passed trivially are now discriminating:
  the caller-facing `code` is pinned to the audit code it must derive
  (`zeroth.<condition>` ↔ `<condition>_error`, with `PolicyViolation` branched
  separately), and the content-leak probe now smuggles a secret through an
  unallowlisted `execution_metadata` key instead of asserting on a field the
  secret was never written to.

## [0.13.1] - 2026-07-26

### Added

- Tool-governance vocabulary for the LangGraph integration:
  `_tool_types.py` declares `SideEffectClass`, `ToolDecisionKind`,
  `InventoryCoverage`, `ToolIdentity`, `ToolAction`, `ToolDecision`,
  `ToolGovernanceContext`, `ToolInventoryEntry` and `ToolInventory` as frozen
  dataclasses and `StrEnum`s. `ToolDecisionKind` carries the third verdict
  (`require_approval`) *locally*, so the platform-wide `PolicyDecision` stays
  ALLOW/DENY and no `is PolicyDecision.ALLOW` branch turns a pending approval
  into a hard failure.
- `_tool_errors.py` with `ToolGovernanceError` and its four subclasses
  (`PolicyViolation`, `GovernanceContextError`, `ApprovalRequiresThreadError`,
  `UnstableToolIdentityError`), each carrying a `zeroth.`-namespaced caller
  facing `code`.
- Audit reason codes for those failures, without which a tool denial writes a
  record whose reason is summarized away rather than retained: `policy_violation`
  joins the denial reason codes (a verdict a governance stage returned), and
  `tool_governance_error`, `governance_context_error`,
  `approval_requires_thread_error` and `unstable_tool_identity_error` join the
  package failure codes.
- `tests/integrations/langgraph/tools/test_tool_reason_codes.py` drives each of
  those exceptions through `AuditRepository.write` and asserts the reason code
  survives into storage, with an unregistered probe as the negative control. The
  registration check walks `_tool_errors.__all__`, so an exception added later
  fails the suite until its code is registered too.

## [0.13] - 2026-07-26

### Added

- `langchain` (`>=1,<2`) joins the `gateway-conformance` dependency group. The
  LangGraph tool-enforcement surfaces are built on the agent middleware that
  ships in `langchain` proper — `AgentMiddleware`, `wrap_tool_call`, and
  `ToolCallRequest` — which the already-pinned `langgraph` packages do not
  provide. The resolution moves nothing else: `langchain-core` stays where it
  was, so the existing gateway conformance pins are untouched.
- `tests/integrations/langgraph/tools/` test package, the home for the
  tool-enforcement suites that land on top of this dependency.

## [0.12.4.5.1] - 2026-07-25

### Fixed

- `capture_vocabulary` claimed that `tests/governance/audit/test_capture_vocabulary.py`
  "pins each mirror against the enum it mirrors so the two cannot drift". That file
  did not exist, so the hand-transcribed vocabularies were unpinned. A drift here
  fails *open* in the direction that matters: a new enum member is summarized away,
  so a real decision silently stops being retained and the audit row loses evidence
  it is meant to keep. The test now exists and imports both sides — tests are not
  bound by the layering rule that forces the transcription in the first place.
  The comment's reasoning was also wrong for two of the mirrors: `PolicyDecision`
  lives in `governance` itself and `ApprovalDecisionType` in `contracts`, both
  importable; only `SandboxStrictnessMode` (`integrations`) and `GovernanceLevel`
  (gateway package) genuinely have to be copied. Corrected to say so.

## [0.12.4.5] - 2026-07-25

### Fixed

- Closed the startup-failure hole the bounded-shutdown reorder opened. Moving the
  gateway stop and the audit drain *inside* the runtime lifespan is what made
  them bounded — outside it, the drain queued behind every unbounded post-yield
  await (a run worker's graceful shutdown, the ARQ consumer and pool, the webhook
  client, the secret provider), so one hung predecessor postponed it for as long
  as the hang lasted, which is the lost backlog R10 forbids reached by a slower
  route. But a startup that fails never reaches the body, and an inner-only
  shutdown therefore never ran at all: the transport the bootstrap had already
  built was left open and the accepted audit backlog was left unpersisted and
  unreported. `service_lifespan` now runs the stop and the drain for that case
  through an outer guard, and — because a normal shutdown already ran them —
  exactly once either way. A transport close that fails while a startup is
  already failing is logged under a fixed code and an exception type, never
  re-raised over the startup error an operator actually needs.

### Added

- Failing-before probes for both halves of that guard: a runtime lifespan that
  refuses to start still closes its transport exactly once and still drains the
  events already accepted, a close that fails during a failed startup never masks
  it and never puts its own message in the log, and a normal shutdown does not
  close a transport the inner stop already closed. The existing hung-predecessor
  and abandoned-close probes round out the ordering suite.

## [0.12.4.4] - 2026-07-25

### Fixed

- Realigned the audit-capture suites with the collision-free canonical mapping.
  A canonical mapping is now an entry *sequence* (`{"<entries>": [[key, value],
  …]}`) rather than a flat `{hashed_key: type_name}` dict, because unsafe and
  non-string keys all rendered to the same `***REDACTED***` marker and therefore
  overwrote one another: two submitted entries were persisted as a count of one
  and a schema missing a branch, which broke R4's promise that the hashes and
  counts of dropped content are retained. Five assertions still described the old
  flat shape and were updated to the new one; each still fails if the property it
  was written for breaks. The `operation` assertion moved with the metadata
  vocabulary — its text is filled partly from a client-supplied protocol method,
  so it is now replaced by a stable correlating digest rather than retained.

### Added

- Failing-before probes for the three capture gaps that shipped without one:
  a registered secret nested inside a `set` (the container type none of the three
  stacked walkers traversed), a label-shaped credential filed under `reason_code`
  / `decision` / `status` (a shape check is not a provenance check), and an
  artifact reference whose `store`, `content_type` and nested `metadata` used to
  ride into a metadata-only capture alongside the one key retention needs. Each
  probe leaks its seeded value against the pre-fix source and is clean against
  the current one. A collision probe pins the entry-sequence fix directly: two
  entries whose keys both canonicalize unsafely must count as two, and must
  digest differently from one of them alone.

## [0.12.4.3.1] - 2026-07-25

### Fixed

- Restored the retention suite's opt-in to content capture. Collapsing capture to
  a single unconditional point at `AuditRepository.write` left the retention
  fixtures stamping a capture marker onto each seeded record — a genuine opt-in
  before the seal was deleted, a no-op that still read like one afterwards. The
  seeded PII those eleven tests assert is present *before* a sweep was therefore
  stripped at write time, and the pre-sweep assertions failed. The fixtures now
  install a content-retaining `CaptureClassifier` on the repository via
  `configure_capture`, which is the surviving legitimate mechanism: a deployment
  picks between two fixed outcomes and cannot author the transform, and no record
  influences whether it is captured. The marker-stamping helper is deleted rather
  than left in place under a name it no longer earns.

### Fixed

- Made "decision metadata is retained" true rather than merely claimed. The
  metadata-only capture keeps an allowlisted, per-key-typed projection of
  `execution_metadata`, but every denial producer filed its verdict in a *nested*
  dict (`admission`, `enforcement`, `extra`, `response`) or in the free-form
  `error` the capture replaces — so admission denials, per-branch identifiers,
  enforcement modes, retry counts and cache dispositions all vanished at the
  durable write. Producers now promote those onto flat, allowlisted keys:
  `admitted`, `admission_digest`, `decision`, `reason_code`, `network_mode`,
  `sandbox_strictness_mode`, `branch_id`, `branch_index`, `attempt` and
  `disposition`. `AdmissionController.admit` returns a stable `snake_case`
  reason code on every branch instead of a rendered message.
- `collect_policy_events` was silently inert: it substring-matched
  `denied`/`forbidden`/`policy` against `NodeAuditRecord.error`, which capture
  replaces with a fixed redaction marker, so both `/evidence` endpoints returned
  an always-empty `policy_events` list. It now reads the structural decision
  fields, and each event names the node, the decision and the reason code.
- Retention erasure lost its join key: `join_key` was not on the metadata
  allowlist, so `RetentionErasureService` could not find the econ events an
  erased run produced. `join_key` and the branch identifiers are retained
  verbatim under a new `MetadataKind.IDENTIFIER` — hashing them would satisfy a
  presence check while breaking every consumer that compares them.

### Added

- `MetadataKind.IDENTIFIER`, for the ids this codebase mints from a record's own
  identity, and `normalize_reason_code`, which renders a producer's failure or
  denial *name* as a stable label-shaped code.

## [0.12.4.2] - 2026-07-25

### Fixed

- Collapsed audit capture to a single point. `AuditRepository.write` is now the
  sole, unconditional capture boundary: it always applies the capture policy and
  reads nothing off the record it is handed. Capture previously ran twice — once
  on the delivery worker and once on the durable write — which forced the second
  pass to recognise the first pass's work through a marker on producer-supplied
  `execution_metadata`. A caller could obtain a genuine marker, alter the content
  around it, and have the repository skip capture entirely, bypassing R3/R5.

### Removed

- `zeroth.governance.audit.capture_seal` in full — the per-process nonce,
  `seal_metadata`, `is_sealed`, `strip_seal`, `capture_for_write` and
  `CAPTURE_SEAL_KEY`. With one capture point there is nothing to attest and
  nothing to forge. `CAPTURE_METADATA_KEY` moved to
  `zeroth.governance.audit.capture_policy`.
- The delivery worker no longer classifies or redacts; `AuditDeliveryQueue` no
  longer accepts `classifier`, `redaction` or `known_secrets`, and
  `DeliveryFailure.CAPTURE_FAILED` is gone. `redaction` and `known_secrets` were
  dropped along with the worker's policy and have **no** configuration path:
  `AuditRepository.configure_capture` takes a classifier only, and the
  repository builds its own `AuditCapturePolicy` with the default redaction
  chain and no registered secrets. `AuditRecordWriter` implementations are now
  required to be capture-applying durable sinks; production injects only
  `AuditRepository`.

## [0.12.4.1.1] - 2026-07-25

### Fixed

- Regenerated the console's derived contracts, which the ZER-5 audit-delivery work
  left stale: `frontend/openapi.json` and `frontend/app/lib/api-types.ts` were
  missing the additive `AuditDeliveryHealth` schema and the `audit_delivery` block
  on `HealthResponse`, and `frontend/app/lib/version.ts` still carried the previous
  version. Both drift gates (`docs` → `check:api`, `verify-extras` → `check:version`)
  were failing on `main` as a result.

## [0.12.4.1] - 2026-07-25

### Fixed

- The durable audit write is now the capture boundary. `AuditRepository.write`
  applies the capture policy itself, so the orchestration runtime, the approvals
  service and the service API can no longer reach storage around it: node
  prompts, results, error text, policy denials and approval audits are
  classified and redacted before the digest is computed, whatever the producer
  submitted and whether or not a secret resolver was ever injected (R3, R4, R5).
  A record produced by a capture policy in this process carries a process-local
  nonce and is written unchanged, so the delivery stage's output is not
  transformed twice; the nonce is stripped before the write, so it never reaches
  storage and cannot be replayed. `AuditRepository.configure_capture` installs a
  deployment's classifier once, at wiring time, for deployments that opt into
  retaining content.
- Allowlisted `execution_metadata` keys are now projected per key by declared
  kind -- number, boolean, digest, bounded lower-case label, or an opaque
  identifier that is only ever hashed -- instead of accepting any short scalar.
  A credential pasted into the client-supplied `X-Correlation-ID` header is no
  longer persisted verbatim (R4, R5).
- Mapping key text supplied by a producer is never persisted in a dropped-content
  schema: schema keys are truncated SHA-256 digests, and schema type names come
  from a closed set. An identifier-shaped credential used as a mapping key was
  previously stored as a "shape" (R5).
- The capture redaction chain masks mapping keys as well as values, and the
  payload sanitizer accepts only exact `str` keys instead of calling `str(key)`.
  A registered secret used as a mapping key survived content-mode capture (R5).
- `AuditDeliveryQueue.submit` rejects a whitespace-only tenant and counts a
  dedicated `invalid_record` rejection before raising, so a refused event is
  visible on `/v1/metrics` and `/health` instead of disappearing with every
  counter at zero (R7, R8, R9).
- Each audit write attempt now runs under a finite deadline; an attempt that
  overruns it is cancelled, released without being awaited, counted, and retried
  under the same `audit_id`. One hung write previously owned the stage's only
  worker indefinitely, with no retry, no failure and no counter movement.
  `zeroth_audit_delivery_in_flight_seconds` reports the age of an outstanding
  attempt.
- Shutdown claims an in-flight event's terminal state before cancelling, so a
  write that lands after abandonment is recorded under
  `zeroth_audit_delivery_reconciled_total` rather than counted a second time as
  delivered. "Abandoned" and "delivered" are mutually exclusive again (R7).
- The service lifespan bounds the gateway transport's shutdown and drains the
  audit-delivery queue in an unconditional `finally`. A transport close that
  raised skipped the drain entirely and one that hung postponed it forever,
  losing the accepted backlog; the transport error is now preserved and re-raised
  only after the drain has reported (R10).
- `AuditGatewayEventSink.emit` counts a refused or unprojectable terminal event
  through the delivery stage's counters and returns, instead of raising into the
  gateway proxy's generic handler -- which logged a full traceback per refusal on
  the response-completion path, exactly under saturation. `GatewayAuditRefusedError`
  is removed.

### Changed

- Artifact references owned by a run (`{run_id}/...`) are retained as addressing
  under the capture marker, so retention can still find and destroy a run's
  artifacts after its content channels are emptied.
- `AuditRecordSubmitter` documents as a hard requirement that an implementation
  must be non-blocking and synchronous at the hand-off; the proxy's
  `asyncio.timeout` around the sink is a cooperative guard and cannot make a
  blocking or cancellation-swallowing sink safe.

## [0.12.4] - 2026-07-25

### Added

- The gateway's audit-delivery stage is now built by the service factory with the
  application `MetricsCollector`, so `zeroth_audit_delivery_queued_total`,
  `_retried_total`, `_rejected_total`, `_failed_total`, `_abandoned_total` and the
  `zeroth_audit_delivery_queue_depth` gauge are rendered by `GET /v1/metrics`
  instead of accumulating on a private collector nothing scrapes (R7).
- `GET /health` carries an `audit_delivery` block (queue depth, the per-outcome
  counts and `losing_events`), and `GET /health/ready` carries a matching
  `audit_delivery` dependency check that degrades readiness when the stage has
  lost an event. A loss is never rendered as a delivery (R8).
- `ServiceBootstrap.audit_delivery_queue` exposes the stage the gateway event
  sink submits into, giving it a lifecycle owner outside the sink.

### Changed

- `service_lifespan` now drains the audit-delivery queue during teardown, after
  the gateway transport has stopped and while the audit repository is still
  open, and logs every `audit_id` the bounded drain could not persist rather
  than abandoning it silently (R10).

## [0.12.3.5.1] - 2026-07-25

### Fixed

- Restored the audit capture/redaction test names that the ZER-5 requirement
  manifest pins as proof for R3, so the requirement checks resolve again.

## [0.12.3.5] - 2026-07-25

### Fixed

- `AuditDeliveryQueue` no longer accepts a `capture_policy`. The stage constructs
  its own `AuditCapturePolicy` and applies it before every write; callers may
  still inject a `classifier` and widen `redaction`/`known_secrets`, but they
  cannot substitute a pass-through for the redaction transform (R3).
- A capture transform that raises no longer kills the delivery worker. The worker
  falls back to `blank_record()` and, if even that fails, counts and names the
  event as failed before continuing (R3, R7).
- Metadata-only capture now projects `execution_metadata`, approval metadata and
  every free-form error string through an allowlist
  (`capture_projection.ContentFreeProjection`). Unrecognised keys, containers and
  error text are replaced by a digest, a schema and a count instead of surviving
  best-effort key redaction (R4).
- Dropped-content schemas no longer render mapping keys with `str(key)`. Keys are
  gated by exact type, length and shape, and digests are computed from a
  canonical form that never invokes user `__str__`/`__repr__`, so a payload that
  previously produced `sha256=None` now always produces a digest (R4, R5).
- Capture, classifier and writer failures are logged as a fixed error code plus
  the exception type. Exception messages, which can carry the value that caused
  them, no longer reach the log stream (R5).
- `AuditRepository.write` raises `DuplicateAuditIdError` (a `ValueError`
  subclass, so existing callers are unaffected) and the delivery queue counts
  only that type as "already delivered". A pre-commit validation `ValueError` is
  now retried and then failed rather than reported as a durable write (R8).
- `aclose()` retains the ids of events that exhausted their retries, includes
  them in the close report with `drained=False`, and keeps `failed` distinct from
  `abandoned` (R7, R10).
- `aclose()` applies one absolute deadline to the drain *and* the worker
  cancellation, so a writer that swallows `CancelledError` is reported instead of
  awaited past the bound; the writer contract now requires cancellability (R10).
- Concurrent `aclose()` calls share one close task and one cached report, so an
  in-flight event can no longer be abandoned and counted twice (R7).
- Retry delays and the shutdown timeout reject `NaN` and infinity.
- The delivery stage's task set is now a real supervised registry with a done
  callback that retrieves task exceptions.

### Changed

- Extracted `zeroth.governance.audit.capture_projection`, `delivery_state`,
  `delivery_worker` and `errors` from `capture_policy.py` and `delivery.py`.

## [0.12.3.4] - 2026-07-25

### Changed

- The LangGraph gateway's audit sink (`AuditGatewayEventSink`) now hands terminal
  events to `AuditDeliveryQueue.submit()` instead of awaiting
  `AuditRepository.write()` from inside the streaming response's `finally`. The
  durable write, its retries and its backoff run on the delivery worker, so a
  slow or wedged audit repository can no longer stall, truncate or reorder a
  streamed body; a refused hand-off raises `GatewayAuditRefusedError`, which the
  proxy counts in `sink_failure_count` rather than dropping silently. The
  `audit_id` is minted once per terminal event and fixed on the record before the
  hand-off, so every delivery attempt rewrites the same identity and a retry
  after a partially succeeded write is absorbed by the append-only duplicate
  check. Deriving it from `correlation_id` was rejected: that value can be
  client-supplied, so two unrelated requests could collapse into one record.
- Tenancy is explicit on the audit delivery path: the sink always threads
  `correlation.tenant_id` onto the record, and `AuditDeliveryQueue.submit()` now
  rejects a blank `tenant_id` with `ValueError`. The delivery worker has no
  ambient tenant, and `"default"` is both the model default and the reserved
  tenant owning the fallback retention policy, so an absent tenant would be
  misattributed and given the wrong TTL without surfacing anywhere. No field was
  added to `NodeAuditRecord`.

## [0.12.3.3] - 2026-07-25

### Added

- Capture classification and redaction on the audit delivery path
  (`zeroth.governance.audit.capture_policy`). `AuditDeliveryQueue` now applies
  `AuditCapturePolicy` itself, once per event and before the first write
  attempt, so no record -- on the first attempt, on a retry, or on a retry after
  a partially succeeded write -- can reach the durable writer uncaptured.
  Content capture is off by default: prompt, argument and result values
  (`input_snapshot`, `output_snapshot`, `validation_results`,
  `condition_results`, `stdout`, `stderr`, each tool call's `arguments` and
  `outcome`, each memory interaction's `value`) are emptied and replaced by a
  SHA-256 digest, a key-and-type schema and an entry count filed under
  `execution_metadata["audit_capture"]`, while identity, `status`, timing,
  token usage, cost, actor, approval actions and the digest-chain fields are
  retained. The default is fail-closed in both directions: a queue built with
  no policy still redacts, and a policy that fails mid-transform emits a record
  stripped of every content channel rather than the submitted one. Redaction
  reuses `PayloadSanitizer`, `SecretRedactor` and `PIIFilter`; no field was
  added to `NodeAuditRecord`.

## [0.12.3.2] - 2026-07-25

### Added

- Bounded audit-event delivery stage (`zeroth.governance.audit.delivery`). It sits
  between an audit-event producer and the append-only audit write: `submit()` is a
  synchronous `put_nowait` onto a finite queue, so a slow or wedged writer can never
  suspend a producer, and a saturated queue rejects the newest event with accounting
  instead of growing. A single worker retries each event with jittered exponential
  backoff, reusing the `audit_id` minted at submit time, so a retry persists exactly
  one record -- the repository's duplicate-`audit_id` `ValueError` is read as "already
  durable", not as an error. Queued, delivered, retried, rejected, failed and
  abandoned events are counted on an injectable `MetricsCollector`, and
  `aclose(timeout=...)` drains within a bound and names what it could not deliver.

## [0.12.3.1.2] - 2026-07-25

### Fixed

- The GenAI mapper no longer looks metadata up by key. A `Mapping` lookup runs the
  stored key's `__hash__`/`__eq__`, so a `str` subclass key could execute its own
  code inside the mapper -- spoofing an allowlisted name, or raising an exception
  carrying arbitrary text (`CausalSpan` filters metadata by value type only, so
  such a key survives). Entries are now iterated and `type(key) is str` is checked
  before any comparison.

## [0.12.3.1.1] - 2026-07-25

### Fixed

- The GenAI mapper now gates the `tags` *container* type, not just its entries: a
  `tuple` subclass could override `__iter__` and yield entries it never stored,
  injecting them into `langgraph.tags`. `CausalSpan` normalises only `metadata`,
  so such a container reaches the mapper intact; anything but an exact `tuple`
  is now omitted.

## [0.12.3.1] - 2026-07-25

### Fixed

- Replayed causal trees no longer attach to the ambient span. `emit_genai_spans`
  starts every root — and every orphan, whose dangling parent makes it a root —
  against an explicitly empty OpenTelemetry `Context` instead of inheriting
  whatever span happens to be active at emit time. The records are historical
  (their start/end are past `perf_counter` readings), so inheriting the ambient
  span misattributed a replayed tree to an unrelated caller and made two
  independent roots siblings of one trace, breaking the one-trace-per-tree
  guarantee. Children still carry their in-batch parent's context.
- Blank strings are treated as absent throughout the GenAI mapper. An empty or
  whitespace-only value for the resolved target/name, `thread_id`
  (`gen_ai.conversation.id`), `correlation_id` (`zeroth.correlation_id`), an
  allowlisted metadata string or a tag entry now omits the attribute entirely
  rather than emitting `""`; a span whose only target source is blank falls back
  to an operation-only span name. Integers are unaffected — `langgraph.step=0` is
  still emitted.
- Exact-type gates at the mapper boundary. Every value admitted into a span
  attribute, the span name, the OTel status or `MappedGenAiSpan` is checked with
  `type(x) is str` / `type(x) is int` rather than `isinstance`, so a `str`
  subclass can no longer override `__format__` / `__str__` / `__repr__` to inject
  unrelated content while itself reaching a `gen_ai.*` attribute. Optional values
  failing the gate are dropped; the structural identity (`run_id`,
  `parent_run_id`, `kind`, `status`) cannot be dropped without silently
  reparenting an orphan, so such a record is rejected before any span is started.
  Untrusted `perf_counter` readings yield no `duration_ns` or `start_time`.

## [0.12.3] - 2026-07-25

### Added

- Versioned mapper from the neutral LangGraph causal-span records to standard
  OpenTelemetry GenAI spans (ZER-4). `map_causal_span` is pure and imports no
  OpenTelemetry: it resolves `gen_ai.operation.name` from the record's kind
  (`tool` → `execute_tool`, `llm`/`chat_model` → `chat`, root `chain` →
  `invoke_workflow`, nested `chain` → `invoke_agent`), names the span
  `"{operation} {target}"`, and emits three disjoint namespaces — standard
  `gen_ai.*` identifiers only, `langgraph.*` ancestry and structure, and
  `zeroth.*` governance metadata that keeps the unverified gateway correlation id
  out of `gen_ai.*`. Every span is stamped with the mapping's
  `zeroth.convention_version`, so consumers pin the attribute shape by that value
  alone and a bump never touches the collection contract.
- `emit_genai_spans` rebuilds the real parent/child span tree on the OpenTelemetry
  SDK, starting each child with its parent's context and treating a parent absent
  from the batch as a root. Batch topology is validated before the first span is
  started (empty or duplicate run ids and parent cycles are rejected) and
  emission order is computed in one iterative pass, so a deeply nested or
  reverse-ordered batch cannot recurse. Timestamps are never fabricated: mapped
  spans expose only a `perf_counter`-derived `duration_ns`, and absolute times
  are derived solely from an explicit caller-supplied clock anchor. Exported
  lazily, so importing the integration package still works without the `otel`
  extra.
- Privacy is structural rather than configurable: the records carry no prompts,
  tool arguments or results, so the mapper has no content channel and
  deliberately no `capture_content` switch. Golden fixtures and exporter
  integration tests pin the emitted attribute sets, the allowlisted-metadata type
  gates (`bool` and `str` subclasses rejected), and the absence of any content
  channel; a drift test compares the vendored `gen_ai.*` constants against
  `opentelemetry.semconv` whenever it is importable.

## [0.12.2.4] - 2026-07-23

### Fixed

- Governed LangGraph streaming now scopes the correlation `ContextVar` to each
  individual chunk pull rather than holding it across `yield` (ZER-3 audit-3): the
  correlation is reset before a chunk reaches the consumer, so it never leaks into
  the caller's context, an abandoned iterator leaks nothing, interleaved streams
  cannot observe each other's correlation, and every token stays confined to the
  context that created it (closing a stream from another task no longer raises).
  `astream` additionally closes the delegate iterator it wraps, so early close and
  cancellation run the delegate's own cleanup.

## [0.12.2.3] - 2026-07-23

### Fixed

- ZER-3 causal-ancestry audit-2. Correlation now rides a wrapper-owned
  `ContextVar` that only the langgraph integration package sets and reads,
  replacing the `config["metadata"]["zeroth_correlation_id"]` channel (F8,
  security). Metadata is caller-reachable — via `config["metadata"]`, a
  callback-manager's `metadata`/`inheritable_metadata`, or a preceding callback
  mutating it at runtime — so sanitizing each path was whack-a-mole; the carrier
  is now never exposed to callers and caller metadata can no longer forge the
  gateway correlation. `GovernedGraph` sets the carrier around each run (reset in
  `finally`) and wraps the returned `stream`/`astream` generators so it is
  published at iteration start and reset at exhaustion/cancellation, preserving
  chunk order, laziness and cancellation. The extract-only, unverified token
  parsing (8 KiB cap + `RecursionError` guard) is unchanged. `CausalSpan` now
  always copies its incoming metadata instead of retaining a caller's
  `MappingProxyType` (whose backing dict stays mutable) and drops non-scalar
  values defensively (F9).

## [0.12.2.2] - 2026-07-23

### Fixed

- ZER-3 causal-ancestry audit hardening (audit-1). Closed a correlation trust-
  boundary hole: the `govern_graph` wrapper now strips any caller-supplied
  `metadata["zeroth_correlation_id"]` before injection and re-sets it only from a
  valid gateway `_zeroth` token, so an untrusted caller can no longer forge the
  gateway correlation (absent/malformed token now yields `None`, never the
  caller's value). Bounded reserved-context token parsing against denial of
  service — oversized tokens are rejected by length before decoding and
  `RecursionError` from a deeply nested payload is swallowed to `None` instead of
  propagating out of graph invocation. Span names now resolve from the callback
  `name` kwarg then the serialized runnable/tool/model name before falling back
  to the LangGraph node, so a nested sub-runnable keeps its own name and tool/LLM
  spans are no longer nameless. `CausalSpan.metadata` is now an immutable
  `MappingProxyType`, so a consumer cannot mutate a returned span or defeat the
  metadata whitelist. The LLM callbacks mirror the pinned langchain-core 1.5.0
  signatures (`tags` on `on_llm_end`/`on_llm_error`, upstream `LLMResult` /
  chunk / `BaseMessage` types). Added stronger dedup (full run id vs shared
  prefix) and live concurrent-read coverage.

## [0.12.2.1] - 2026-07-23

### Fixed

- ZER-3 causal-ancestry orphan classification. The `govern_graph` governance
  handler previously classified a start whose `parent_run_id` was `None` as an
  `orphan` whenever another root was still open on the shared handler — so two
  genuinely concurrent (or sequential) top-level runs through the one handler
  instance could mislabel a legitimate root as an orphan depending on
  scheduling, corrupting ancestry. Now every `parent_run_id is None` start is a
  root (many roots coexist cleanly on one handler), and an `orphan` is instead a
  span whose non-`None` `parent_run_id` names a run id that was never observed (a
  dangling reference). Orphan is determined at read time (in the
  `completed_spans` / `open_spans` accessors) against every observed run id, not
  frozen at the start callback — so an out-of-order child delivered before its
  parent resolves to the real parent, while a truly dangling parent is marked
  `orphan` (status override) and never reparented to a root.

## [0.12.2] - 2026-07-23

### Added

- Causal LangGraph callback ancestry capture (ZER-3): the `govern_graph`
  governance handler now reconstructs a run's causal `run_id` / `parent_run_id`
  tree into neutral, OpenTelemetry-agnostic `CausalSpan` records (kind, status,
  timings, tags and a whitelisted structural-metadata subset) held in an
  in-memory sink. Concurrency-safe under a single shared handler: lock-guarded
  state keyed by full run ids (never truncated), one span per run id, exactly
  one terminal per run, and detached starts flagged `orphan` rather than
  reparented. The wrapper also carries the gateway correlation id onto every
  span by extracting it (extract-only, unverified) from the reserved-context
  token and merging it into `config["metadata"]`, riding LangGraph's native
  metadata inheritance. Capture only — no delivery/persistence (ZER-5) and no
  OpenTelemetry mapping (ZER-4).

## [0.12.1.3] - 2026-07-23

### Fixed

- `govern_graph` callback-manager governance normalization (ZER-2 audit-3): when a
  `BaseCallbackManager`'s `handlers` and `inheritable_handlers` lists hold divergent
  governance-handler instances, or pre-existing duplicates, the merge now collapses
  them to a single canonical governance identity present exactly once in each list,
  instead of leaving the lists divergent.

## [0.12.1.2] - 2026-07-23

### Fixed

- `govern_graph` with_config chaining and manager inheritance (ZER-2 audit-2):
  - Chained `GovernedGraph.with_config(...)` now shallow-overwrites previously
    bound top-level keys (tags/metadata/configurable/callbacks/run_name/...)
    wholesale, matching `RunnableBinding.with_config`, instead of merging and
    accumulating them. `merge_configs` still layers the bound config under the
    call config at invoke time, unchanged.
  - Governance callback-manager dedup now keeps exactly one governance handler in
    **both** `handlers` and `inheritable_handlers` (same identity), regardless of
    which list a pre-installed handler started in — so governance always
    propagates to child runs and is never duplicated.
  - Broadened the audit gate tests: telemetry-failure safety is now exercised
    across all four entrypoints (invoke/stream/ainvoke/astream) with the failing
    transport asserted to actually run, plus chained-with_config equivalence and
    asymmetric callback-manager layouts.

## [0.12.1.1] - 2026-07-23

### Fixed

- `govern_graph` transparency and callback-merge hardening (ZER-2 audit-1):
  - `GovernedGraph.with_config(...)` now stays governed. Previously it delegated
    through `__getattr__` and handed back a bare, ungoverned `RunnableBinding`,
    silently dropping governance. It now returns a governed wrapper that binds the
    config into every run while preserving attribute delegation and the
    `on_run_start` seam. `|` composition raises a clear, actionable error.
  - Callback-merge dedup keys on governance-handler **type/identity**, never on
    user-callback equality. Nested `govern_graph(govern_graph(g))` or a
    pre-installed handler no longer double-installs governance, and a user
    callback with a hostile `__eq__` can no longer suppress the Zeroth handler
    (both list and `BaseCallbackManager` config shapes).
  - Econ telemetry emission in `InstrumentedLangGraph` is now best-effort: a
    failing transport can never replace the graph's result, mask its exception,
    or alter cancellation.

## [0.12.1] - 2026-07-23

### Added

- `govern_graph`, a transparent observed-mode wrapper around a compiled
  LangGraph, exported from `zeroth.integrations.langgraph`. One-line install
  (`graph = govern_graph(graph)`) reuses the econ instrumentation delegation for
  cost capture and merges a Zeroth governance callback handler into each run's
  config without replacing or duplicating user callbacks. Results, streamed
  chunks and exceptions are byte-for-byte equivalent to the bare graph. The
  wrapper honours the FA5 capability floor — it mints no attestation and adds no
  path that promotes a run above `admission` (promotion to `observed` is
  deferred) — and exposes an optional no-op `on_run_start` stability seam.
  Importing the package never imports the optional `langgraph` dependency.

## [0.12.0] - 2026-07-21

### Added

- Rebuilt operations console, including Studio, deployment, governance,
  optional Regulus views, and destination-only Webhooks integration.
- Platform-admin-only, allowlisted Regulus proxy with deterministic generated
  platform and Regulus API clients.

### Changed

- The durable structured-token engine is now selected for unauthored graphs.
  Explicit `sequential_join_enabled=False` remains the temporary warned legacy
  compatibility escape hatch; immutable deployment engine pins still win.

### Fixed

- Structured loop boundary delivery, nested lifecycle cancellation, graceful
  stop, replay, checker topology/schedule coverage, and deployment pinning
  release blockers.

## [0.11.1] - 2026-07-20

### Added

- **B9 token/provenance join engine (P1–P3, flag-off), ported from
  `feat/b9-join-loops`** (branch versions v0.11.5.2–v0.11.6.1 + subsequent
  oracle/replay/diamond commits), reimplemented on the decomposed runtime.
  Replaces the loop-epoch join model with provenance tags that travel with
  the token:
  - `runtime/orchestration/token_scope.py` (new): pure static loop analysis —
    DFS back-edges, natural-loop bodies, enclosing loops, exit edges and
    their outermost-owner units, tag propagation.
  - `GraphDriver`: tag-keyed join buckets (per-iteration re-join on loops),
    back-edge re-entry dispatch, loop-exit edges resolved only by the
    exit-crossing event (whole unit at the outer tag), skip cascade with
    dead-loop exit suppression, durable in-flight dispatch records
    (stage/restore) so a failed node replays its exact payload and tag, and
    declared-shape JoinConfig merges (`collect` default, `merge_path`).
  - Validation: `IRREDUCIBLE_LOOP`, `MULTI_LATCH_LOOP`, `FANOUT_IN_LOOP`
    (parallel-config-in-loop + structural token forks with the
    reconvergence-before-boundary proof), `FANOUT_SUCCESSOR_JOIN`; the
    `JOIN_ON_CYCLE` rejection is removed — loops now join correctly
    (`contracts/graph/validation/token_loops.py`, rewritten `joins.py`).
  - Models: `ParallelConfig` gains `max_concurrency`/`batch_size`/
    `branch_timeout_seconds` (wave + worker-pool + per-branch timeout
    execution in `runtime/parallel/executor.py`); `JoinConfig` default flips
    to non-lossy `collect` and gains `merge_path`.
  - The monolith's overridable orchestrator seams are preserved through the
    decomposition: `_dispatch_node`, `_record_forward_resolution`,
    `_stash_join_payload`, `_merge_join_payloads`, `_back_edge_ids` route
    driver-internal calls through the facade so subclasses (incl. the
    trace/oracle bridge) observe the real engine. Loop-analysis caches live
    on the facade instance and are threaded into each driver.
  - Test suites ported: extended `test_join_barrier{,_stress}.py` (nested
    loops, loop-then-combine, multi-exit, bypassed-loop shapes), new
    `test_token_scope.py`, `test_token_engine_model.py` (reference oracle),
    `test_token_engine_runtime_trace.py` (real-runtime trace bridge incl.
    seeded corruption), `test_failed_node_replay.py`; parallel executor
    batching/concurrency/timeout suites.
  - Surface fixtures re-amended additively (`ParallelConfig`, `JoinConfig`,
    `RuntimeOrchestrator` cache fields).

## [0.11.0.0.1] - 2026-07-20

### Fixed

- Formatting-only: collapse a wrapped list comprehension in
  `runtime/context/tracker.py` (ruff format; missed in the v0.10.6 port
  commit).

## [0.11] - 2026-07-20

### Added

- **B9 — sequential join barrier (feature-flagged, default OFF), ported from
  the public main line** (main v0.11 `924ab54` + v0.11.0.1 deadlock guard
  `d012200`), reimplemented on the decomposed runtime. An opt-in dispatch
  subsystem fixing diamond payload corruption: an unconditional convergent
  node previously executed twice and clobbered its merged input. Gated by
  `ExecutionSettings.sequential_join_enabled` (default False — flag-off
  behavior byte-identical, verified by the unchanged characterization pins).
  - New `JoinConfig` model (same merge vocabulary + reducer registry as
    `ParallelConfig`) and `NodeBase.join_config`
    (`contracts/graph/models.py`).
  - Publish validation: `MISSING_JOIN_CONFIG` on genuine concurrent delivery
    without a merge policy; `JOIN_ON_CYCLE` rejects convergent-on-cycle
    graphs (new `contracts/graph/validation/joins.py`, wired into the
    contract validator after cycle checks).
  - `GraphDriver` gains the barrier: `run_branch_planner` (full plan with
    suppressed edges), `edge_payload` (shared payload/mapping computation),
    `advance_downstream` (single sequential post-node entry point), and the
    join worklist (delivered/suppressed edge resolution, skip cascade,
    `join_state` checkpoint round-trip, JoinConfig merge, cyclic-edge
    defense). The approval-resolution path advances through the same entry
    point.
  - Deadlock guard: leftover `join_state` at completion fails the run loudly
    (`join_deadlock`) instead of silently completing past a join waiting on
    an unreachable inbound edge.
  - Test suites ported: `tests/orchestrator/test_join_barrier.py` (24 tests,
    incl. the documented shared-schema merge-clobber xfail) and
    `test_join_barrier_stress.py`.
  - **Surface fixture amendment (first use of the additive-amendment policy,
    now documented in `docs/backend-import-migration.md`):**
    `ExecutionSettings` and `NodeBase` (+ its six node subclasses) gain the
    new optional fields in both surface fixtures — 32 signature updates plus
    the `JoinConfig` registration on both surfaces; the legacy shim
    `zeroth.core.graph.models` re-exports `JoinConfig`.

## [0.10.6] - 2026-07-20

### Fixed

B-series orchestrator/infra fixes ported from the public main line (main
v0.10.1.17, v0.10.1.19, v0.10.1.20, v0.10.1.23, v0.10.1.24, v0.10.1.25.1) —
this completes the port of main's entire 29-release `0.10.1.x` audit series:

- **B5/B6/B7 — econ-plane query 500s + dollar-denominated counterfactual**
  (`econ/plane/{capabilities,counterfactual,performance}/service.py`), with
  the new `tests/econ_plane/test_service_query_fixes.py` suite.
- **B11 — sidecar no longer double-applies network config**
  (`integrations/sandbox/executor.py`).
- **B12 — Vault token refresh** on expiry with single-flight re-login
  (`platform/secrets/vault.py`).
- **B13 — signal handling left to uvicorn.** The lifespan's
  `loop.add_signal_handler` block overrode uvicorn's own SIGTERM/SIGINT
  handlers, so `should_exit` never set and the process hung until SIGKILL
  (`service/bootstrap/lifecycle.py`).
- **B1/B3 — prompt no longer re-renders audit; context tracker counts tool
  calls** (`runtime/agents/prompt.py`, `runtime/context/tracker.py`).
- **B8 — nested approval pause on a RESUMED fan-out branch persists
  durably** via the same pause handler as the first pause, instead of an
  in-memory re-queue that was lost on reload (`runtime/orchestration/
  driver.py`).
- **B10 — best_effort fan-out fails loud on multi-branch approval pause**
  (new `MultipleBranchPauseError`) instead of silently orphaning all but the
  last paused branch (`runtime/parallel/{errors,executor}.py`).

## [0.10.5] - 2026-07-20

### Fixed

Security-hardening fixes ported from the public main line (main v0.10.1.12,
v0.10.1.15, v0.10.1.18, v0.10.1.20.1, v0.10.1.21, v0.10.1.22):

- **S5 — condition evaluator blocks dunder attribute access** on non-Mapping
  objects, closing the `__class__`/`__globals__` introspection gadget path
  (`contracts/conditions/evaluator.py`).
- **S7 — subgraph resolution is tenant-scoped.** `SubgraphResolver.resolve`
  (and the `DeploymentLookup` protocol seam) take `tenant_id`/`workspace_id`;
  all four call sites (executor initial + resume, driver resume path,
  parallel-executor fallback) pass the parent run's tenant, so a subgraph
  node naming a foreign tenant's deployment ref fails closed.
- **S3/S4 — rate-limit and quota check-and-update serialized.** Token-bucket
  and quota transactions take `write_lock=True` (+ `FOR UPDATE` on Postgres);
  cold-start insert uses `ON CONFLICT DO NOTHING` + re-read, so concurrent
  first requests can't 500 or double-spend a capacity-1 bucket
  (`governance/guardrails/rate_limit.py`).
- **S2 — strict sandbox refuses the local backend unconditionally** (was a
  silent no-op for bare inline units with no resource constraints); STANDARD
  behavior unchanged (`integrations/execution/sandbox.py`).
- **B4 — run worker never leaks a concurrency slot.** The lease-renewal task's
  own exception (e.g. "database is locked") no longer escapes the `finally`
  before `release_lease` + semaphore release
  (`runtime/orchestration/run_worker.py`).
- **S6 — failure text routed through the secret redactor.** Both persisted
  audit `error` columns and `RunFailureState.message` (returned verbatim by
  the public run API) are redacted; no-op without a secret resolver
  (`runtime/orchestration/{audit_recorder,driver}.py`).

## [0.10.4] - 2026-07-20

### Fixed

- **F3 — cooperative run cancellation, full series ported from the public
  main line** (main v0.10.1.6, v0.10.1.8, v0.10.1.11, v0.10.1.25). The drive
  loop holds an in-memory `Run` and blind-writes `RUNNING` every hop, so an
  operator's out-of-band cancel (`FAILED`) or interrupt (`WAITING_INTERRUPT`)
  was clobbered and the run drove to completion. `GraphDriver.external_stop`
  re-reads the persisted status at the loop head and before every `RUNNING`
  write (ordinary nodes, sync/resumed subgraphs, parallel and resumed
  fan-ins); it adopts the operator's status onto the in-memory run and
  persists it (so `pending_node_ids` survives for replay), and yields to a
  concurrent operator replay/resume rather than blind-writing the stale
  status back. The refactor-era characterization pins gain the new
  `run.get` observations — a deliberate contract change, not drift.

## [0.10.3] - 2026-07-20

### Fixed

Erasure + audit-integrity fixes ported from the public main line (main
versions v0.10.1.5, v0.10.1.10, v0.10.1.14, v0.10.1.16):

- **F1 — erasure left plaintext PII behind.** `redact_run` now clears every
  free-form column that can hold PII: `execution_history` (per-node
  input/output snapshots), `failure_state` (whose message re-derives `error`
  on read, so nulling `error` alone resurfaced the plaintext),
  `condition_results`, `channels`, and `pending_approval` (requester reason +
  metadata). The pre-erasure payload harvest decrypts
  `run_checkpoints.state_json` before parsing — previously every
  at-rest-encrypted deployment hit `JSONDecodeError` and the erasure rolled
  back to a silent no-op. On this tree the fix lands in
  `integrations/persistence/runs/retention_queries.py` (with a `decrypt`
  seam) and `run_repository.py` passes the checkpoint store's
  `decrypt_state_json`.
- **S1 — audit verifier accepted forged erasures.** `erased` is
  digest-excluded and a v2+ erased record's digest folds in stored
  commitments, so a DB-only attacker could flip `erased=True` and rewrite PII
  without breaking digest or signature. The verifier now refuses any record
  claiming to be erased while still carrying populated PII commitment fields
  (`governance/audit/verifier.py`).

## [0.10.2] - 2026-07-20

### Fixed

Tenant-isolation audit fixes ported from the public main line (main versions
v0.10.1.2–v0.10.1.4, v0.10.1.7, v0.10.1.9, v0.10.1.13):

- **F4 — cross-tenant IDOR in the tenant cost/budget API.**
  `GET /v1/tenants/{id}/cost` and `PUT /v1/tenants/{id}/budget` now enforce
  `require_resource_scope`: a principal may only read its own tenant's spend or
  set its own tenant's cap (previously any tenant admin could read or zero-out
  another tenant's budget by path id). Follow-up: `GET
  /v1/deployments/{ref}/cost` is scoped to the served deployment.
- **F8 — cross-tenant IDOR in the webhook API.** Subscriptions are bound to
  the served deployment + tenant (body `deployment_ref`/`tenant_id` are no
  longer trusted); list/get/delete are scoped to the served deployment;
  dead-letter list/replay are scoped via the deployment's own subscription
  set **in the query** (LIMIT applies after the tenant scope), and replay
  carries an ownership guard so a foreign dead-letter reads as 404.
  `WebhookRepository.list_dead_letters` gains a `subscription_ids` IN-clause
  filter; `WebhookService.get_dead_letter` is exposed for the guard.
- **F7 — missing RBAC gate on the econ-plane costing router.** Pricing
  catalog and cost-profile writes are Admin-only; reads require any econ role
  — matching every sibling econ router (previously the router carried no auth
  dependency at all).

## [0.10.1] - 2026-07-20

### Added

- **Console F-series wave + econ portfolio dashboard, ported from the public
  main line** (main versions v0.10.0.0.4–v0.10.1.1). The frontend adopts the
  console audit wave wholesale: attestation panel + deployment rollback
  (F12/F6), run controls + quality-verdict + contract form (F3/F5/F11),
  tenant budget card (F4), evidence export + deployment chain verify (F2),
  Retention & Compliance page (F1), Integrations/webhooks page (F8), and
  Prompt templates page (F9). All backend endpoints these pages call already
  exist on this line; `openapi.json` and `api-types.ts` were regenerated from
  this tree's app and the frontend typechecks clean.
- **Console-reachable econ portfolio dashboard via proxy (F7).** New
  `zeroth.service.api.econ_dashboard_api` proxies the bundled Regulus
  read-only dashboard views under `/v1/econ/dashboard/*` behind
  `METRICS_READ`, using the same server-side self-auth bridge as `cost_api`.
  Registered on both the `/v1` and compat routers; covered by
  `tests/service/test_econ_dashboard_api.py`. The refactor-era contract
  snapshots (`backend_openapi.json`, `backend_route_inventory.json`) are
  regenerated for the 9 additive routes — a deliberate surface extension, not
  drift.

### Removed

- Dead econ-plane statistics router (F14, main v0.10.0.1.1): the 3-line
  `statistics/api.py` stub and its two `main.py` registration lines. The
  `statistics.schemas` models (pinned surface) are untouched.

## [0.10.0.1] - 2026-07-19

### Fixed

- `AsyncSQLiteDatabase.transaction()` no longer fails with a spurious
  `CoordinationTimeoutError` when several fresh connections race the one-time
  delete→WAL journal-mode conversion on a new database. SQLite's
  deadlock-avoidance path returns `SQLITE_BUSY` from
  `PRAGMA journal_mode = WAL` without consulting the busy timeout, so the
  pragma is now retried within the coordination-timeout budget. Coordination
  semantics (`BEGIN IMMEDIATE` write lock, `CoordinationTimeoutError`
  contract) are unchanged.

## [0.10.0.0.1] - 2026-07-13

### Changed

- Docstrings and comments across the memory, agent-runtime, contracts,
  execution-units, and runs modules reworded from "GovernAI" to "governed" now
  that the framework slice is vendored in-tree (`zeroth.core.governed`). No
  behavior change. The public `GovernAIRedisRuntimeStores` /
  `build_governai_redis_runtime` identifiers and the `governai_kind` contract
  keys are retained for compatibility.

## [0.10] - 2026-07-13

### Changed

- **Absorbed the `governai` dependency in-tree.** Zeroth used only a curated slice
  of the external `governai==0.2.3` framework (memory types + connector wrappers,
  `RunState`/`RunStatus`, tool primitives, tool-call helpers, flow/step spec types,
  audit emitters); that slice is now vendored under `zeroth.core.governed` and the
  external dependency is dropped. Behavior is unchanged — a pure move, proven by the
  full suite and the `ScopedMemoryConnector` `SHARED → "__shared__"` isolation
  invariant. governai's execution kernel (`runtime/local`, `workflows`, `approvals`,
  `policies`, `agents`, `sandbox`) was intentionally left behind; zeroth runs its own
  orchestrator. Transitive deps `lark`, `langgraph-sdk`, and `ormsgpack` are shed.

### Removed

- `GovernedLLMProviderAdapter` (unused; the production LLM path is
  `LiteLLMProviderAdapter`), along with the `integrations.llm.GovernedLLM` binding.

## [0.9.1.2] - 2026-07-13

### Fixed

- **Studio saves no longer wipe node governance fields** — the canvas
  round-trip dropped `capability_bindings`, `policy_bindings`,
  `execution_config`, `audit_config`, and `parallel_config` on every
  structural save, silently stripping API-authored capability restrictions
  (security-relevant under v0.9's enforce-by-default). The Studio API now
  emits these fields in node `data` and, on save, preserves the stored value
  for any key the payload omits (an explicit value, including `[]`, still
  overrides). `node_version` and non-title display metadata also carry over
  instead of resetting.

## [0.9.1.1] - 2026-07-13

CI-gate hotfix for the 0.9.1 release candidate; no behavior changes.

### Fixed

- Docstring coverage restored above the CI `interrogate` gate (the v0.9/v0.9.1
  hardening code shipped under-documented helpers and API models).
- `docs/reference/configuration.md` regenerated to match current settings
  (regulus default-enabled, `FAIL_CLOSED`, `PER_RUN_CAP_USD`, secrets,
  provenance, and retention sections).

## [0.9.1] - 2026-07-13

v0.9 hardening pass — closes the findings of the 2026-07-12 audit across six
workstreams. Bug-fix release: no new product surface.

### Fixed

- **Runtime isolation** — concurrent runs no longer share mutable agent-runner
  state: each dispatch gets a dispatch-local runner (config, provider, memory
  resolver, context tracker, budget enforcer, tool executor), eliminating
  prompt/template crossover and wrong-tenant cost attribution under load.
- **Tenant-safe deployments** — deployment create/rollback/list are scoped to
  the authenticated principal's tenant; graphs carry workspace ownership, and
  Studio-authored deployments can no longer fall back to the `default` tenant.
- **Database coordination** — audit-chain appends serialize through database
  coordination rows (Postgres advisory locks / SQLite reserved rows), so
  multi-worker deployments cannot fork the audit hash chain; durable audit
  sequence numbers backfill in one statement; mixed/legacy chains recover.
- **Retention correctness** — `run_ttl_seconds` is enforced (previously
  persisted but ignored); TTLs validate as positive; audit TTL no longer
  erases whole runs with newer records; legal holds and erasure serialize
  race-free per tenant; erasure cleanup state is materialized instead of
  replaying the full retention log per operation.
- **MCP hardening** — declarative `mcp_servers` config now reaches generated
  runners; agents must hold `process_spawn` + `external_api_call` BEFORE an
  MCP server subprocess is spawned (enforced at dispatch, reported at publish
  validation as `missing_mcp_capability`).
- **Vault hardening** — secret resolution is async, pooled (one shared
  `httpx.AsyncClient`), and single-flight per key and per AppRole login; LLM
  key resolution, signing bootstrap, HTTP auth headers, and execution-unit
  secret injection all resolve off the event loop; the provider closes once
  at app shutdown.
- Repo-root test residue (`econ_plane.db`, `.zeroth/`) is isolated to session
  temp directories, removing an order-dependent test flake.

### Changed

- README, SECURITY.md, and `.planning/PROJECT.md` updated so product claims
  match implemented behavior (deployment restart caveat, budget enforcement
  requires the `regulus` extra, bare-install fail-open, current Next.js and
  embedded-econ-plane architecture).

## Pre-0.9 development series (2026-07-05 – 2026-07-12)

Versions `0.3` – `0.9` shipped in rapid succession from a private working
branch; their scope is summarized here from git history rather than
reconstructed as full entries.

## [0.9] - 2026-07-12

- Governed+secure parity: tenant isolation, capability enforcement, signed
  provenance, retention/right-to-erasure, pluggable secret provider (Vault).

## [0.8] - 2026-07-10

- Economic viability suite (cost-per-successful-outcome, waste rollups,
  model right-sizing) and the Regulus control plane absorbed into the
  `zeroth` namespace (`zeroth.econ_plane`, mounted at `/regulus`).

## [0.7] - 2026-07-08

- Agent tool edges (attached units as callable tools), message-list input,
  persistent conversations, per-node economics on the cost page, and the
  vendor-dd full-surface reference app.

## [0.6] - 2026-07-08

- Code node (author Python on the canvas, sandbox-executed), entrypoint
  node, and user-defined JSON-Schema contracts in the console.

## [0.5] - 2026-07-07

- Runtime connector management, publish/deploy from the API and console
  (the canvas→run loop closes), hardened Docker sandbox defaults.

## [0.4] - 2026-07-06

- Econ-plane auth holes closed, Studio/cost RBAC, console onboarding
  (guide page, workflow templates, inline help), console packaged as the
  `[console]` extra, full-screen Studio canvas, run-from-canvas replay.

## [0.3] - 2026-07-05

- Regulus economic control plane bundled in-repo; console UX quick wins;
  first public release lineage of the `0.x` series.


## [0.2.0] - 2026-04-13

v4.0 Platform Extensions — production-grade agentic workflow capabilities.

### Added

- **Subgraph composition** — nest published graphs as SubgraphNodes with governance inheritance, thread participation modes, approval propagation, and cycle detection (90 tests).
- **Parallel fan-out/fan-in** — split execution across branches with budget isolation, configurable merge strategies, and SubgraphNode-in-parallel guard (55 tests).
- **Template registry** — CRUD REST API for prompt templates with audit-safe secret redaction, `TemplateRegistry.delete()` method (59 tests).
- **Context window management** — token tracking with pluggable compaction strategies and thread persistence (68 tests).
- **Resilient HTTP client** — retry with exponential backoff, circuit breaker, rate limiting, and connection pooling (62 tests).
- **Artifact store** — content-addressed storage with TTL refresh, GET REST endpoint (auth-protected).
- v4.0 concept documentation pages for all six new subsystems.
- OpenAPI spec synced with v4.0 endpoints (artifact GET, template CRUD).
- Phase 39 manual verification tests with real SQLite persistence.

### Changed

- README updated with v4.0 capabilities section and architecture diagram.
- `TemplateRegistry` DELETE endpoint now uses `registry.delete()` instead of accessing private `_templates` dict.
- REQUIREMENTS.md traceability table populated with all 36 v4.0 requirement IDs.
- Configuration reference docs regenerated for new settings.

### Fixed

- E501 line-too-long in `parallel/executor.py`.
- I001 unsorted imports in `service/app.py`.

## [0.1.1] - 2026-04-11

First public PyPI release of `zeroth-core`, the governed multi-agent runtime
library extracted from the Zeroth platform. This release establishes the
OSS-grade metadata, optional dependency extras, and trusted-publisher release
pipeline required for a stable PyPI presence.

### Added

- Apache-2.0 `LICENSE` file at repo root (canonical text).
- `CHANGELOG.md` in keepachangelog 1.1.0 format.
- `CONTRIBUTING.md` with dev setup, PR conventions, issue filing, and license guidance.
- PEP 561 `py.typed` marker under `src/zeroth/core/` — downstream users now receive type hints out of the box.
- `[project.urls]` block in `pyproject.toml` (Homepage, Source, Issues, Changelog).
- PyPI classifiers and keywords for searchability and discoverability.
- `examples/hello.py` — minimal runnable fixture (PKG-06 acceptance) that proves a clean-venv install of `zeroth-core` produces a working program.
- Optional dependency extras carved out of the base dependency list:
  `[memory-pg]`, `[memory-chroma]`, `[memory-es]`, `[dispatch]`, `[sandbox]`, and `[all]` (PKG-03).
- GitHub Actions trusted-publisher release workflow (`release-zeroth-core.yml`) with TestPyPI staging, clean-venv smoke-install gate, and Sigstore attestations (PKG-05).

### Changed

- Dependencies carved into a minimal base plus six optional extras. Installing
  bare `zeroth-core` no longer transitively pulls `psycopg`, `pgvector`,
  `chromadb-client`, `elasticsearch`, `redis`, or `arq` — each backend is
  opt-in via its extra.
- Build backend bumped to `hatchling>=1.27` for PEP 639 SPDX
  `license-expression` support.
- Hatchling wheel target verified for the PEP 420 namespace layout introduced
  in Phase 27. The existing `packages = ["src/zeroth"]` target was kept
  unchanged after confirming it produces a correctly-rooted `zeroth/core/`
  wheel with no stray top-level `zeroth/__init__.py`.

[Unreleased]: https://github.com/rrrozhd/zeroth-core/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/rrrozhd/zeroth-core/releases/tag/v0.1.1
