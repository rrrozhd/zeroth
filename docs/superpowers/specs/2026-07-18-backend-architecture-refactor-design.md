# Backend Architecture Refactor Design

## Objective

Restructure Zeroth's backend into explicit runtime, governance, platform,
contracts, service, economics, and integration domains. Decompose the largest
implementation files, reduce repeated infrastructure helpers, and remove only
provably unreachable or superseded code. Preserve runtime behavior, service
contracts, persistence formats, and useful library capabilities while allowing
documented import-path changes.

The frontend is explicitly out of scope. A separate frontend pull request is in
flight and no file under `frontend/` will be modified by this work.

## Constraints

- Zeroth is both a deployable service and a library for building governed AI
  applications. A symbol is not dead merely because the service does not call
  it.
- Public behavior, HTTP contracts, database schemas, serialized models, and
  exception semantics must remain stable.
- Public import paths may change when the new ownership is clearer. Each such
  change must be recorded in a backend migration guide.
- Existing documented library APIs should be preserved where practical.
- Every independently testable refactor slice receives an atomic commit after
  its focused tests pass.
- User-owned changes in the original worktree, especially `.planning/` and
  `.claude/`, must remain untouched.

## Target Architecture

The long-term backend ownership model is:

```text
src/zeroth/
  runtime/
    orchestration/
    agents/
    parallel/
    subgraphs/
  governance/
    approvals/
    audit/
    policy/
    guardrails/
    retention/
  platform/
    artifacts/
    dispatch/
    observability/
    primitives/
    secrets/
    storage/
  contracts/
    graph/
    registry/
    mappings/
    templates/
  service/
    api/
    bootstrap/
  econ/
    analytics/
    instrumentation/
  integrations/
    http/
    rag/
    sandbox/
```

This is a staged migration rather than a single tree-wide move. Files are first
decomposed behind clear interfaces, then moved with their cohesive domain when
the dependency direction is explicit. Service and integrations may depend on
runtime, governance, contracts, platform, and economics; those domains must not
depend on service code.

## Runtime Decomposition

`RuntimeOrchestrator` remains the primary orchestration entry point but becomes
a composition root over focused collaborators:

```text
RuntimeOrchestrator
  |-- GraphDriver
  |-- NodeDispatcher
  |-- ParallelExecutor
  |-- PolicyGate
  |-- RuntimeAuditRecorder
  `-- RuntimeToolExecutor
```

- `GraphDriver` owns run and resume state-machine progression.
- `NodeDispatcher` resolves node types and delegates execution.
- `ParallelExecutor` owns fan-out, fan-in, pause, and resume mechanics.
- `PolicyGate` owns policy checks and side-effect approval consumption.
- `RuntimeAuditRecorder` owns run history and failure audit construction.
- `RuntimeToolExecutor` owns governed tool invocation wiring.

The facade continues to expose the orchestration operations, but collaborators
receive explicit dependencies rather than reaching back into a broad mutable
orchestrator object. Extraction must not alter ordering, pause/resume behavior,
audit payloads, policy decisions, or failure handling.

## Validation Decomposition

`GraphValidator` remains the public validation entry point and delegates to
focused validators for:

- graph and node references;
- node-type rules;
- entrypoints;
- edges and tool edges;
- tool attachments and capability grants;
- conditions and mappings;
- cycle detection.

All validators append the existing `ValidationIssue` representation and retain
the current deterministic issue ordering. The public report and exception
behavior do not change.

## Persistence Decomposition

The runs package separates these responsibilities:

- run persistence and transitions;
- thread persistence and resolution;
- checkpoint persistence and lookup;
- row/model serialization;
- retention and erasure queries.

`RunRepository` and `ThreadRepository` remain domain-facing repository APIs.
Shared transaction-sensitive operations live in focused internal stores rather
than a single thousand-line repository module. Transaction ownership and lock
scope must remain unchanged.

## Retention Decomposition

Retention erasure is separated into:

- orchestration of run, audit, and tenant purges;
- cleanup manifest construction and replay;
- cleanup claim leasing and heartbeats;
- cleanup operation execution;
- result construction and compatibility logging.

The claim fencing, idempotency, replay, and terminal-record semantics are safety
boundaries. Characterization tests must cover them before their extraction.

## Service Bootstrap Decomposition

Service bootstrap separates configuration resolution, dependency construction,
lifecycle management, migrations, and route installation. Route modules remain
domain-focused. Repeated authorization, error translation, and response logic
may be consolidated only where behavior is exactly equivalent; no generic route
factory is introduced merely to reduce line count.

## Shared Primitives and DRY

Repeated UTC clock and identifier helpers move into
`zeroth.platform.primitives`. Time-sensitive governance, leasing, and retention
code continues to accept an injectable clock where deterministic behavior is
important. Domain-specific identifier formats remain owned by their domains;
only genuinely identical generation logic is shared.

Duplication is removed when the repeated code expresses the same policy and
changes for the same reason. Superficially similar validation, persistence, or
route code is not unified if doing so would obscure domain behavior.

## Dead-Code Standard

A symbol may be deleted only when all relevant evidence shows that it is:

1. unreachable through direct imports and calls;
2. not dynamically registered or loaded by name;
3. not exported as part of a useful library surface;
4. not documented or demonstrated as a supported capability;
5. not represented in service schemas or compatibility contracts; and
6. either functionally redundant with a maintained implementation or incapable
   of providing additional behavior.

Framework callbacks, protocol methods, public models, optional integrations,
and schema-facing types are presumed live until proven otherwise. Candidate
removals happen in a final, separate phase with individual evidence recorded in
the commit or migration guide.

## Migration Documentation

A backend migration guide maps old documented import paths to new paths and
calls out removed legacy implementations. It distinguishes import-only changes
from behavioral changes; this refactor is not expected to include intentional
behavioral changes.

## Testing Strategy

The baseline is the all-extras environment:

```bash
uv sync --all-extras
uv run pytest -q
```

The pre-refactor baseline is 1,973 passed, 16 deselected, and three warnings.

For each extraction:

1. add or identify characterization coverage for the boundary;
2. introduce a failing boundary/import test when a new collaborator or package
   does not yet exist;
3. implement the smallest extraction that passes it;
4. run the focused domain tests;
5. run Ruff on affected source;
6. commit the independently passing slice.

After each domain migration, run its broader test group. Final verification is:

```bash
uv run pytest -v
uv run ruff check src/
uv run ruff format --check src/
```

An import-surface smoke test will exercise the documented backend library entry
points and the new package locations. No completion claim is made without fresh
full-suite and lint evidence.

## Delivery Order

1. Establish architecture checks and migration documentation scaffolding.
2. Introduce shared primitives where equivalence is proven.
3. Decompose and migrate orchestration runtime.
4. Decompose and migrate graph validation.
5. Decompose and migrate runs and thread persistence.
6. Decompose and migrate retention erasure.
7. Decompose service bootstrap and API ownership.
8. Move remaining cohesive backend packages into their target domains.
9. Audit and remove only proven dead or superseded code.
10. Run full verification and complete migration documentation.

Each step is allowed to refine the exact module split when tests or dependency
analysis reveal a safer boundary, but it may not broaden scope into frontend or
intentional behavior changes.
