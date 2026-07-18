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
- Existing library capabilities, call signatures, return semantics, and
  exception semantics must be preserved. The permitted compatibility break is
  an import-location change documented in the migration guide.
- Every independently testable refactor slice receives an atomic commit after
  its focused tests pass.
- User-owned changes in the original worktree, especially `.planning/` and
  `.claude/`, must remain untouched.

## Target Architecture

The following tree is the authoritative long-term backend ownership model:

```text
src/zeroth/
  runtime/
    orchestration/
    agents/
    context/
    parallel/
    runs/
    subgraphs/
  governance/
    approvals/
    audit/
    identity/
    policy/
    guardrails/
    retention/
  platform/
    artifacts/
    config/
    dispatch/
    observability/
    persistence/
    primitives/
    secrets/
    signing/
    storage/
  contracts/
    conditions/
    governed/
    graph/
    registry/
    mappings/
    templates/
  service/
    api/
    bootstrap/
    deployments/
    webhooks/
  econ/
    analytics/
    instrumentation/
    plane/
  integrations/
    execution/
    http/
    memory/
    rag/
    sandbox/
  eval/
```

This is a staged migration rather than a single tree-wide move. Files are first
decomposed behind clear interfaces, then moved with their cohesive domain when
the dependency direction is explicit. Service and integrations may depend on
runtime, governance, contracts, platform, and economics. Runtime communicates
with integrations through runtime-owned protocols and must not import concrete
integration modules. No backend domain may depend on service code.

### Current-to-target module map

| Current package | Target package | Disposition |
| --- | --- | --- |
| `core.orchestrator`, `core.agent_runtime`, `core.parallel`, `core.subgraph`, `core.context_window` | `runtime.orchestration`, `runtime.agents`, `runtime.parallel`, `runtime.subgraphs`, `runtime.context` | Move and decompose |
| `core.approvals`, `core.audit`, `core.policy`, `core.guardrails`, `core.retention`, `core.identity` | `governance.approvals`, `governance.audit`, `governance.policy`, `governance.guardrails`, `governance.retention`, `governance.identity` | Move; decompose retention |
| `core.artifacts`, `core.dispatch`, `core.observability`, `core.secrets`, `core.storage`, `core.signing`, `core.config` | `platform.artifacts`, `platform.dispatch`, `platform.observability`, `platform.secrets`, `platform.storage`, `platform.signing`, `platform.config` | Move; add `platform.primitives` |
| `core.graph`, `core.contracts`, `core.mappings`, `core.templates`, `core.conditions` | `contracts.graph`, `contracts.registry`, `contracts.mappings`, `contracts.templates`, `contracts.conditions` | Move; decompose validation |
| `core.runs` models and repository protocols | `runtime.runs` | Runtime-owned run/thread/checkpoint domain contracts |
| `core.runs` SQL persistence and row serialization | `platform.persistence.runs` | Concrete persistence; decompose repositories |
| `core.service`, `core.deployments`, `core.webhooks` | `service.api`, `service.bootstrap`, `service.deployments`, `service.webhooks` | Move; decompose bootstrap |
| `core.econ`, `econ_plane` | `econ.analytics`, `econ.instrumentation`, `econ.plane` | Consolidate without changing optional capabilities |
| `core.http`, `core.rag`, `core.execution_units`, `core.sandbox_sidecar`, `core.memory` | `integrations.http`, `integrations.rag`, `integrations.execution`, `integrations.sandbox`, `integrations.memory` | Move; preserve optional integration surfaces |
| `core.eval` | `eval` | Move as a stable library capability |
| `core.governed.app`, `core.governed.models` | `contracts.governed` | Consolidate governed specifications and models |
| `core.governed.runtime`, `core.governed.tools` | `runtime.orchestration`, `runtime.agents` as appropriate | Merge legacy governed runtime capabilities into maintained runtime boundaries |
| `core.governed.audit`, `core.governed.memory`, `core.governed.integrations` | `governance.audit`, `integrations.memory`, and the relevant integration package | Consolidate only after capability inventory and supersession evidence |
| `core.demos`, `core.examples`, migrations | Existing locations until a separately justified change | Explicitly unchanged by this refactor |
| `zeroth.core` package shell and top-level CLI/entry points | Existing location during migration; imports rewritten to canonical packages | Thin bootstrap/compatibility shell, not a home for domain implementations |

Package moves may be divided further in the implementation plan, but no
backend package may be moved without appearing in this table or an approved
spec amendment.

### Import direction

Allowed top-level dependencies are:

| Package | May depend on |
| --- | --- |
| `platform` | standard library and third-party infrastructure only |
| `contracts` | `platform` |
| `governance` | `contracts`, `platform` |
| `runtime` | `contracts`, `governance`, `platform` |
| `econ` | `contracts`, `platform` |
| `integrations` | `contracts`, `governance`, `platform`, `runtime`, `econ` |
| `service` | every backend domain; no backend domain may import `service` |
| `eval` | `contracts`, `runtime`, `platform` |

An architecture test scans imports and fails on a disallowed top-level edge.
Temporary exceptions require an explicit allow-list entry with a removal task;
the final refactor leaves no undocumented exception.

### Run and persistence boundary

`runtime.runs` owns the `Run`, `Thread`, checkpoint, history, and status models,
plus repository protocols required by orchestration and agent runtime. Runtime
code depends only on these models and protocols. `platform.persistence.runs`
owns the SQL-backed `RunRepository` and `ThreadRepository`, transaction helpers,
row serialization, retention queries, and concrete checkpoint persistence.
Service bootstrap constructs those implementations and injects them through the
runtime-owned protocols. This keeps persistence replaceable for library users
and prevents a runtime-to-service dependency.

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

Before moves begin, build a library-surface inventory from package `__all__`
exports, public imports, reference documentation, examples, entry points,
schema-exposed types, and optional integrations. The inventory is committed and
becomes the source for import smoke tests.

A backend migration guide maps old library paths to new paths and calls out
removed legacy implementations. Every changed or removed public symbol receives
a row with:

| Field | Meaning |
| --- | --- |
| old path and symbol | Previously supported import |
| new path and symbol | New canonical import, if any |
| disposition | moved, renamed, superseded, or removed |
| compatibility status | whether a temporary re-export exists; none is required |
| replacement | Maintained capability replacing a superseded symbol |
| removal evidence | Dead-code criteria and supporting searches/tests |

Capabilities, signatures, return semantics, and exception semantics remain
mandatory. Import location is the only generally permitted compatibility
change. Any intentional behavioral change requires a separate approved design.

## Testing Strategy

The baseline is the all-extras environment:

```bash
uv sync --all-extras
uv run pytest -q
```

The pre-refactor baseline is 1,973 passed, 16 deselected, and three warnings.

Before implementation, capture explicit contract fixtures or characterization
checks for:

- the generated OpenAPI schema and stable HTTP response/error shapes;
- database schema objects and migration revision ordering;
- round-trip serialization of persisted runs, threads, checkpoints, audit
  records, approvals, and cleanup manifests;
- the inventoried library symbols and their public call signatures; and
- representative exception types and failure semantics at public boundaries.

Golden artifacts should compare semantic structures rather than unstable
formatting. An approved fixture update must be isolated from implementation
changes so contract drift cannot hide inside a refactor commit.

For each extraction:

1. add or identify characterization coverage for the boundary;
2. introduce a failing boundary/import test when a new collaborator or package
   does not yet exist;
3. implement the smallest extraction that passes it;
4. run the focused domain tests;
5. run Ruff on affected source; and
6. commit the independently passing slice.

Every completed implementation step, including package moves, documentation,
architecture checks, and dead-code removals, receives an atomic commit after
its focused tests and Ruff checks pass.

After each domain migration, run its broader test group. Final verification is:

```bash
uv sync --all-extras
uv run pytest -v
uv run ruff check src/
uv run ruff format --check src/
git diff --exit-code <refactor-base> -- frontend/
```

An import-surface smoke test will exercise the documented backend library entry
points and the new package locations. The final diff check proves that the
frontend remained untouched. No completion claim is made without fresh
all-extras, full-suite, lint, formatting, contract-fixture, architecture, import
surface, and frontend-diff evidence.

## Delivery Order

1. Inventory the library surface and capture protected contract fixtures.
2. Establish architecture checks and migration documentation scaffolding.
3. Introduce shared primitives where equivalence is proven.
4. Decompose and migrate orchestration runtime.
5. Decompose and migrate graph validation.
6. Decompose and migrate runs and thread persistence.
7. Decompose and migrate retention erasure.
8. Decompose service bootstrap and API ownership.
9. Move the remaining packages listed in the module map.
10. Audit and remove only proven dead or superseded code.
11. Run full verification and complete migration documentation.

Each step is allowed to refine the exact module split when tests or dependency
analysis reveal a safer boundary, but it may not broaden scope into frontend or
intentional behavior changes.
