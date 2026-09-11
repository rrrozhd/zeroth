# Backend Import Migration Guide

This guide is the change log for public Python import locations during the
backend architecture refactor. The migration completed on 2026-07-20, and the
compatibility shell was **removed in 0.17**.

> **Breaking change in 0.17.** The `zeroth.core` and `zeroth.econ_plane` import
> paths no longer exist. There is no deprecation period and no alias: importing
> either raises `ModuleNotFoundError`. Use the package mappings below to
> find the canonical location for each name you import, and
> `tests/contracts/fixtures/backend_surface_canonical.json` — the executable
> form of this guide — to confirm it.

Everything that moved kept its call signature, return behaviour, and exception
semantics; only the import path changed. Two things also left the wheel in
0.17 and now ship only in the repository: the quickstart tutorial helper
(`examples/quickstart.py`) and the demo scripts (`examples/demos/`).

## Compatibility policy

- A useful library capability may move to a clearer domain package, but its
  call signature, return behavior, and public exception semantics remain
  stable.
- A temporary re-export was optional and is now gone: 0.17 removed every
  compatibility shim, so the canonical import recorded here is the only one.
- `tests/contracts/fixtures/backend_surface_legacy.json` is immutable after
  the corrected baseline inventory is accepted. It identifies protected
  capabilities independently of their future import locations.
- `tests/contracts/fixtures/backend_surface_canonical.json` is evolving. Every
  edit to it must be committed separately from production moves and accompanied
  by an updated package mapping or contract amendment in this guide.
- Superseded or removed symbols require explicit dead-code evidence covering
  static reachability, dynamic registration, exports, documentation, examples,
  service schemas, and optional integrations.

### Protected fixture maintenance

Private implementation cleanup and frontend-only cleanup do not change either
backend-surface fixture. There is no generator or regeneration step for those
changes: the accepted legacy fixture remains immutable, and the canonical
fixture remains untouched when no protected capability moved or disappeared.

When a protected capability intentionally moves, preserve its `legacy_ids`,
update its canonical module and name mapping, add the corresponding row to the
symbol migration log, and run the backend surface contract tests. Removing a
protected capability requires evidence that covers static and dynamic
reachability, exports, documentation, examples, service schemas, and optional
integrations before its canonical mapping can be removed.

### Paid-product contract amendments

The owner approved D01 (unknown costs and explicit evidence contracts) and D02
(exploratory claims with an explicit quality floor) on 2026-09-06. These amendments
change economic behavior after the import migration; they do not promise the old
zero defaults or undeclared outcome maturity remain sufficient evidence. No symbol
moves or disappears. Existing execution constructors now expose all their validated
fields through Pydantic's normal signature, including optional runtime attribution.

The immutable legacy fixture stays unchanged. The canonical fixture records the
following signatures and eight D01 amendment receipts, each binding the exact old
identity/signature and current identity/signature with SHA-256. An absent, stale or
unrelated receipt fails the gate. Updating another signature requires its own
reviewed contract change; these receipts are not blanket exemptions. Canonical
fixture and guide record the accepted contract changes explicitly.

| Protected legacy identity | Canonical identity | Approved contract change |
| --- | --- | --- |
| `zeroth.core.econ.instrumentation:ExecutionEvent` | `zeroth.econ.instrumentation:ExecutionEvent` | D01: expose the validated evidence fields; missing costs are `None`, with the declared 18-digit, 8-decimal-place bounds. |
| `zeroth.core.econ.instrumentation.schemas:ExecutionEvent` | `zeroth.econ.instrumentation.schemas:ExecutionEvent` | D01: same execution class and contract. |
| `zeroth.econ_plane.instrumentation.schemas:ExecutionEventCreate` | `zeroth.econ.plane.instrumentation.schemas:ExecutionEventCreate` | D01: expose validated evidence fields and actual unknown-cost defaults and bounds. |
| `zeroth.core.econ.instrumentation:OutcomeEvent` | `zeroth.econ.instrumentation:OutcomeEvent` | D01: nullable outcome value and explicit maturity, defaulting to unknown. |
| `zeroth.core.econ.instrumentation.schemas:OutcomeEvent` | `zeroth.econ.instrumentation.schemas:OutcomeEvent` | D01: same outcome class and contract. |
| `zeroth.econ_plane.instrumentation.schemas:OutcomeEventCreate` | `zeroth.econ.plane.instrumentation.schemas:OutcomeEventCreate` | D01: explicit outcome maturity, defaulting to unknown. |
| `zeroth.econ_plane.instrumentation.schemas:OutcomeQueryResponse` | `zeroth.econ.plane.instrumentation.schemas:OutcomeQueryResponse` | D01: return outcome maturity, defaulting to unknown for older records. |
| `zeroth.econ_plane.instrumentation.schemas:IngestResult` | `zeroth.econ.plane.instrumentation.schemas:IngestResult` | D01/A01: expose the accepted server-owned arrival time as optional `ingested_at`; older receipts remain valid. |

The following models were introduced after the legacy snapshot. Their canonical
signatures remain pinned, with empty legacy mappings rather than invented IDs.

| Canonical identity | Approved contract change |
| --- | --- |
| `zeroth.econ.plane.debugger.schemas:EconomicDiagnosticReport` | D01: expose the count of nonmonetary summary events. |
| `zeroth.econ.plane.cloud.schemas:SdkExecutionEvent` | D01: source-window and charge ownership fields; unknown/unmeasured cost defaults and precision bounds. |
| `zeroth.econ.plane.cloud.schemas:SdkOutcomeEvent` | D01: nullable acceptance and explicit outcome maturity. |
| `zeroth.econ.plane.decisioning.schemas:VersionComparisonRequest` | D01: optional paired source-window inventories. |
| `zeroth.econ.plane.backtesting.schemas:BacktestCostEvidence` | D01: inventory the added observed-usage cost basis, amounts, pricing snapshot and usage by role. |
| `zeroth.econ.plane.backtesting.schemas:BacktestComputation` | D01: include that cost evidence in the computed result. |
| `zeroth.econ.plane.backtesting.schemas:EconomicBacktest` | D01/D02: retain cost evidence, exploratory claim/method/limitations and the `review_candidate` action. |

Owned fractional charges must be supplied as exact decimal strings, not binary
floats. Missing amounts stay unknown; explicit zero stays zero. Declaring measured
cost or final outcome maturity remains the caller's assertion, not certification of
source completeness. See `packaging/sdk/README.md` and `PROJECT_MODEL.md` in the
repository for ingestion, migration and reader rules.

### Evaluation context contract amendment

The 2026-09-11 API contract review accepted the optional `instruction: str | None
= None` keyword on `zeroth.econ.analytics:CorrectnessScorer` (legacy identity
`zeroth.core.econ:CorrectnessScorer`). Existing positional arguments, keyword
defaults and score/error handling remain supported. Omitting the instruction
keeps the case input unwrapped in the judge request; supplying it adds workflow
context alongside the case input without mutating the case.

The canonical receipt uses decision `2026-09-11-api-contract-review` and binds
the exact legacy and current signature pair. It is separate from the earlier
D01 receipts; the immutable legacy fixture and existing mappings are unchanged.

The same review records these additions to models introduced after the legacy
snapshot. All retain empty legacy mappings and accept payloads without the new
fields.

| Canonical identity | Contract addition |
| --- | --- |
| `zeroth.econ.plane.backtesting.schemas:BacktestComputation` | Optional `evaluation_evidence`, defaulting to `None`. |
| `zeroth.econ.plane.backtesting.schemas:EconomicBacktest` | Optional persisted `evaluation_evidence`, defaulting to `None` for older reports. |
| `zeroth.econ.plane.debugger.schemas:BreakagePoint` | `method_version`, defaulting to `legacy_unversioned`. |
| `zeroth.econ.plane.debugger.schemas:CohortPoint` | Same method-version default. |
| `zeroth.econ.plane.debugger.schemas:EconomicDiagnosticReport` | Same method-version default, including independently versioned nested points. |
| `zeroth.econ.plane.debugger.schemas:TimelinePoint` | Same method-version default. |
| `zeroth.econ.plane.reconciliation.schemas:ProviderBillReport` | Same method-version default. |

Older reports do not acquire evidence or a validated method label. Compatibility
is exercised by the rightsizing experiment, backtest decision-rule, economic
debugger and provider-bill reconciliation tests. The backend surface and signature
exclusion tests continue to pin the visible constructors.

### Deferred structural work

This cleanup does not split the oversized run repository, LangGraph tool guard,
or Studio editor/API modules. Consolidating the duplicate child-graph
implementations is also deferred; each is a separate structural change with a
wider regression surface than dead-definition removal.

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
| `zeroth.core.artifacts`, `zeroth.core.config`, `zeroth.core.dispatch`, `zeroth.core.observability`, `zeroth.core.secrets`, `zeroth.core.signing`, `zeroth.core.storage` | `zeroth.platform.artifacts`, `zeroth.platform.config`, `zeroth.platform.dispatch`, `zeroth.platform.observability`, `zeroth.platform.secrets`, `zeroth.platform.signing`, `zeroth.platform.storage` | Move; add shared persistence and primitives | Canonical import path published (Task 11; the governed store factory landed in `zeroth.integrations.persistence.governed_redis` and the run worker in `zeroth.runtime.orchestration.run_worker`) |
| `zeroth.core.conditions`, `zeroth.core.contracts`, `zeroth.core.graph`, `zeroth.core.mappings`, `zeroth.core.templates` | `zeroth.contracts.conditions`, `zeroth.contracts.registry`, `zeroth.contracts.graph`, `zeroth.contracts.mappings`, `zeroth.contracts.templates` | Move; decompose graph validation | Skeleton only |
| `zeroth.core.runs` models and protocols | `zeroth.runtime.runs` | Move domain contracts | Canonical import path published |
| `zeroth.core.runs` SQL persistence | `zeroth.integrations.persistence.runs` | Move and decompose persistence adapters | Canonical import path published |
| `zeroth.core.service`, `zeroth.core.deployments`, `zeroth.core.webhooks` | `zeroth.service.api`, `zeroth.service.bootstrap`, `zeroth.service.deployments`, `zeroth.service.webhooks` | Move; decompose bootstrap | Skeleton only |
| `zeroth.core.econ`, `zeroth.econ_plane` | `zeroth.econ.analytics`, `zeroth.econ.instrumentation`, `zeroth.econ.plane` | Move and consolidate | Skeleton only |
| `zeroth.core.execution_units`, `zeroth.core.http`, `zeroth.core.memory`, `zeroth.core.rag`, `zeroth.core.sandbox_sidecar` | `zeroth.integrations.execution`, `zeroth.integrations.http`, `zeroth.integrations.memory`, `zeroth.integrations.rag`, `zeroth.integrations.sandbox` | Move; preserve optional integrations | Skeleton only |
| `zeroth.core.eval` | `zeroth.eval` | Move stable evaluation capability | Skeleton only |
| `zeroth.core.governed.app`, `zeroth.core.governed.models` | `zeroth.contracts.governed` | Move and consolidate specifications | Skeleton only |
| `zeroth.core.governed.runtime`, `zeroth.core.governed.tools` | `zeroth.runtime.orchestration`, `zeroth.runtime.agents` | Move into maintained runtime boundaries | Skeleton only |
| `zeroth.core.governed.audit`, `zeroth.core.governed.memory`, `zeroth.core.governed.integrations` | `zeroth.governance.audit`, `zeroth.integrations.memory`, relevant integration packages | Move only after capability inventory | Skeleton only |
| `zeroth.core.demos`, `zeroth.core.examples`, `zeroth.core.migrations`, `zeroth.econ_plane._migrations` | Existing locations | Unchanged | Unchanged by this refactor |
| `zeroth.core` package shell and top-level CLI/entry points | Existing locations during migration | Unchanged compatibility shell | Unchanged during staged moves |

## Migrating an import

**Every relocated symbol kept its name.** Across the whole decomposition the moves
were package-level: `zeroth.core.audit:AuditRepository` became
`zeroth.governance.audit:AuditRepository`, not something new. So to migrate a
legacy import, find your package in the dispositions table above and swap the
prefix — the name after the colon does not change.

The gateway relocation (ZER-24) postdates that table; its packages map as:

| Legacy package | Canonical package |
| --- | --- |
| `zeroth.core.langgraph_gateway.models`, `…inventory` | `zeroth.contracts.langgraph_gateway` |
| `zeroth.core.langgraph_gateway.capabilities`, `…events` | `zeroth.governance.langgraph_gateway` |
| `zeroth.core.langgraph_gateway` (admission, compatibility, context, headers, transport, proxy, routes, enforcement service slice, enforcement_store) | `zeroth.service.langgraph_gateway` |
| `zeroth.core.langgraph_gateway.enforcement` (wire-protocol DTOs) | `zeroth.integrations.langgraph.enforcement_protocol` |

The legacy import shims were removed in 0.17. Use the canonical package directly;
old `zeroth.core` and `zeroth.econ_plane` imports raise `ModuleNotFoundError`.

For exact symbol locations and supported signatures, consult
`tests/contracts/fixtures/backend_surface_canonical.json`. Its correspondence
with the protected legacy fixture is checked by
`tests/architecture/test_library_surface.py`. Contract amendments are listed above.

## Updating the canonical surface

For a moved symbol, retain its immutable legacy capability ID in the canonical
entry's `legacy_ids`, change only the canonical `module` and `name`, add the
migration row above, and run both backend contract test modules. Multiple old
IDs may map to one canonical symbol only when the implementations are proven
semantically equivalent.

### Relocating a schema-bearing service module

Service API modules need a specific three-commit order, because
`_discover_schema_models` in `tests/architecture/test_library_surface.py`
selects schema modules by *directory name* — a file counts only when its parent
directory is literally `service`. Moving a module to `zeroth/service/api/`
therefore takes it out of discovery.

The two obvious orderings both fail:

- **Fixture first** is impossible. Canonical rejects duplicate `legacy_ids`, so
  the old and new entry cannot coexist, and
  `test_every_canonical_symbol_imports_and_matches_its_signature` imports every
  entry, so canonical cannot name a module that does not exist yet.
- **Move plus discovery extension in one commit** would repoint discovery to the
  new module path while canonical still records the old one, so the production
  commit fails its own hook unless it also edits the golden fixture.

The order that works, verified on `studio_schemas`:

1. **Production move.** `git mv` the module under `zeroth/service/api/`, leave a
   re-export shim at the legacy path holding no definitions of its own, and
   point in-tree importers at the canonical location. Both paths stay
   importable, so every pinned legacy signature still resolves. Run the module's
   focused gate, the route inventory, the OpenAPI snapshot, and `tests/architecture`.
2. **Docs commit.** Repoint the canonical `module`, plus any `signature` and
   `evidence` strings embedding the old path. Leave `legacy_ids` alone — they
   name the legacy path by definition. Add the migration rows above.
3. **Final Task 10 commit only.** Extend `_discover_schema_models` to cover
   `zeroth/service/api/` and delete
   `tests/architecture/test_service_schema_relocation.py`.

Step 3 had to come last: once discovery covers the new layout, every
*subsequent* module move is discovered under its new path before its fixture is
repointed, which reinstates the deadlock. Task 10 completed this sequence for
all 22 modules; the discovery extension landed in `refactor: compose service
bootstrap`, restoring the reverse-coverage total to the exact pre-refactor 234
models (64 under `zeroth.service`), and the transitional guard was retired in
the same commit. The sequence above remains the template for any future
schema-bearing module relocation.

## ZER-24 — LangGraph gateway relocation, and where `enforcement` landed

ZER-23 classified `zeroth.core.langgraph_gateway` under the backend dependency
policy but left two modules unmapped and two temporary exceptions standing.
ZER-24 relocates the package and removes both exceptions. The mapping table
below records the domain each module went to, the reason, and the resulting
edge count — the same rigor as the ZER-23 table.

| Module | Canonical domain | Why |
| --- | --- | --- |
| `models`, `inventory` | `contracts` | Pure data shapes plus endpoint classification rules. `models` imports only stdlib and pydantic; `inventory` imports only `models`. |
| `capabilities`, `events` | `governance` | Governance-level reporting and audit event emission. |
| `admission`, `compatibility`, `context`, `headers`, `transport`, `proxy`, `routes`, `enforcement` (service slice), `enforcement_store` | `service` | Request-time orchestration and persistence owned by the gateway service. |
| `enforcement` (wire-protocol slice) | `integrations` | See below. |

### Why the enforcement wire protocol went to `integrations`, not `contracts`

`enforcement.py` postdates the ZER-23 mapping table, so its home was explicitly
a ZER-24 decision. It splits along a clean line: a versioned DTO/wire-protocol
slice that `zeroth.integrations.langgraph._gateway_client` consumes, and a
service slice (`LangGraphEnforcementService`, its metrics, its boundary errors)
that only the gateway runs.

The DTO slice was originally planned for `contracts`. That was **wrong on
measurement**: `ActionDescriptorV1`, `DecisionRequestV1`, `InventoryEntryV1` and
`InventoryRegistrationV1` name `SideEffectClass`, `ToolDecisionKind` and
`InventoryCoverage` from `zeroth.integrations.langgraph._tool_types`, and
`contracts` may import only `platform`. Housing the slice in `contracts` would
have replaced the forbidden `integrations → service` edge with a worse
`contracts → integrations` one.

Two repairs were considered and rejected. Moving `_tool_types` into `contracts`
would work on dependencies — it is pure stdlib — but it is a 251-line
non-gateway module with eleven in-tree dependents, which is scope expansion
rather than a consequence of this relocation. Restating the three enums, the
technique `zeroth.governance.decisions.request` uses, would split the identity
of a type that travels the wire between client and server; governance restates
only because it has no legal alternative, whereas integrations importing
integrations is ordinary.

The slice therefore lives at
`zeroth.integrations.langgraph.enforcement_protocol`. The client reaches it
without leaving its own domain, and the gateway service reaches it across the
`service → integrations` edge the policy already permits.

**Resulting forbidden-edge count: zero**, and no exception. To be precise about
what that does and does not claim: the gateway *service* still depends on
`integrations` to reach the protocol, which is an ordinary **permitted** edge
under the policy — the earlier wording here said "not one permitted edge — none",
which overstated it. What ZER-24 drives to zero is the count of **forbidden**
edges and temporary exceptions touching `langgraph_gateway`. The client reaches
the protocol without leaving its own domain, so no edge is created on that side
at all. The distinction is worth stating because exception E2's own
`removal_task` text anticipated "a canonical home in a domain integrations may
import", which does not name `integrations` itself. That text was ZER-23-era
guidance, not the gate. The gate is the dependency scan plus the
exact-exceptions bijection in
`tests/architecture/test_backend_dependencies.py`, and both now assert the empty
set for gateway edges and gateway exceptions.

The companion exception E1
(`zeroth.governance.decisions.service → …langgraph_gateway.admission`) was
removed by inverting the dependency rather than relocating it:
`ToolDecisionService` now requires a governance-owned `AdmissionEvaluator`
(`zeroth.governance.decisions.admission`), and the service domain injects a
`BoundAdmissionEvaluator` that binds the existing `admit` combiner. There is
still exactly one admission combiner; only the direction of the dependency
changed.
