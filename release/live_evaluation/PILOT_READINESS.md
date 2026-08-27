# Zeroth pilot-readiness closure plan

## Bottom line

Revision `c76abd4327cacc76c40cb19a23cd4ccca88f7073` is suitable for a
controlled demo, but a full pilot is **not yet accepted**. The current evidence
supports `demo_ready_not_full_campaign_accepted`: the principal authoring,
execution, approval, loop, batch, audit, Economics, Retention, Webhook,
Artifacts, and bounded Rightsizing paths have direct evidence, while the
remaining pilot blockers are operational cutover, credential rotation, two
more live grounded-research repetitions, full pilot-scope authorization and
recovery checks, security-dependency closure, and a final reconciled evidence
seal.

This document defines the smallest credible bar for a successful pilot. It does
not relabel deferred full-product criteria as passes and does not claim
production readiness.

## Claim levels

| Claim | Meaning | Current state |
| --- | --- | --- |
| Demo ready | A supervised operator can demonstrate the validated workflows on the persistent local instance. | **Supported**, subject to preserving the sealed V3 evidence and current budgets. |
| Pilot accepted | Named pilot users can use an explicitly scoped deployment for a bounded period, with support, recovery, security, isolation, audit, and cost controls proved. | **Not yet accepted**; the gates below remain. |
| Full product-surface accepted | Every item in the immutable 141-criterion campaign catalog passes. | **Not accepted**; the authoritative baseline is 123 pass, 0 fail, 15 blocked, and 3 not run. |
| Production ready | The service meets the deployment, scale, key-custody, availability, and operational requirements of a production environment. | **Not claimed**. Local HMAC proves keyed integrity, not non-repudiation, and the current SQLite/Docker topology is not a distributed production design. |

## Pilot scope to freeze before execution

The pilot owner must record one immutable scope document containing:

- the two disposable tenant IDs, workspace scopes, four role identities, named
  pilot users, start/end dates, and support owner;
- the exact workflows and versions included in the pilot;
- enabled integrations and the explicit prohibition on payment, email,
  production-write, or uncontrolled third-party action APIs;
- expected traffic, data classification, retention period, recovery objective,
  and the `$10.00` campaign / `$0.25` per-run limits;
- success metrics and exit thresholds, including run success rate, approval
  latency, retrieval-quality checks, cost variance, and severity-one incident
  tolerance; and
- the authority to stop, roll back, revoke access, erase disposable data, and
  close the pilot.

Without this freeze, “pilot success” is not measurable and the campaign will
continue expanding into the full-product matrix.

## Required closure gates

| Gate | What is already proved | Work remaining | Pass condition and evidence |
| --- | --- | --- | --- |
| 1. Release candidate and runtime cutover | Local `main` and the evaluation branch point to revision `c76abd43`; the persistent services are healthy. | Build and recreate the pilot services from the reviewed `main` checkout or an immutable image digest. The currently running containers bind `/Users/dondoe/.codex/worktrees/0327/zeroth`, although it presently has the same commit. Freeze graph/deployment identities and take pre-cutover database and external-state snapshots. | Health returns the intended deployment and exact graph version; manifest records commit/image/config/database hashes; restart preserves Runs, Audits, Economics, approvals, artifacts, and connections. Rollback and roll-forward are executed once and screenshot/runtime evidence is sealed. |
| 2. Credential and signing custody | Provider calls are routed through the scoped secret-provider path, and evidence rejects secret-shaped values. | Revoke the credential exposed in chat/diagnostics, install a new pilot credential outside the repository, rotate service credentials where required, record key owners and expiry, and verify without printing values. Keep local HMAC’s trust boundary explicit. | Both tenant services start without secrets in code, Git, logs, screenshots, DOM/network evidence, or bundles; a recursive secret scan passes; old credentials fail; signed audit readiness is `signed`; rotation and emergency revocation are rehearsed. |
| 3. Workflow 1 live reliability | One live grounded run retrieved the approved source, produced the structured answer, invoked the controlled tool once, verified a 6/6 signed chain, and reconciled to `$0.00025859` after the rollup repair. Provider-free negative cases and loop bounds have evidence. | Run live happy repetitions two and three through the UI on the final release candidate. Re-run the pilot-critical negatives: no result/conflict, bad credential, Chroma unavailable, timeout, rate limit, malformed response, and excessive revision. | Three total happy repetitions correlate UI result, retrieval IDs, tool receipt, provider/model/usage, reservation, cost event, Regulus event, and signed audit. Each negative is deterministic, contained, economically reconciled, refresh-restored, and screenshot-backed. No automatic retry follows an ambiguous outcome. |
| 4. Workflow 2 composed execution | Three eight-item live repetitions completed with concurrency four, ordered isolated children, signed chains, unique call identities, and reconciled economics. Child pause/reject, refresh, and partial collection have provider-independent evidence. | Reconfirm one representative eight-item run after the final runtime cutover and exercise pilot-configured cancellation/fail-fast or best-effort behavior. | UI and runtime independently show eight children, indexes 0–7, observed concurrency four, parent/child lineage, selected failure mode, aggregate cost, and refresh restoration. |
| 5. Governed actions and loops | Three approval happy paths, rejection/SLA/ambiguous-outcome handling, exactly-once local action markers, operator resolution, and provider-free loop repetitions are evidenced. | Freeze which local sink and loop demos are pilot-visible; run one post-cutover approval and one Repeat/Done/Limit loop boundary. Decide whether the local action sink is sufficient for pilot scope or replace it with a non-production integration under the same idempotency contract. | Exactly one marker after approval, zero after rejection/cancellation, timeout-after-commit remains ambiguous until authoritative resolution, and max retries are visible in Studio and Runs. No real production action endpoint is called. |
| 6. Rightsizing decision quality | One measured case completed four calls, persisted after refresh, reconciled to `$0.00051700`, and correctly returned `flagged`; it proves plumbing, not model equivalence. | Either keep Rightsizing explicitly advisory during the pilot or run at least the configured `min_cases=5` representative cases with an approved call budget and quality rubric. | Advisory mode visibly prevents an automatic model switch; or a measured result meets the case minimum, preserves per-case verdicts and costs, survives refresh, and is reviewed by an authorized owner. One case must never be presented as a production switch decision. |
| 7. Tenant, role, and data isolation | Twin tenants and operator/reviewer/admin/platform-admin routes have same-tenant and cross-tenant evidence, including scope-hiding 404 behavior. | Execute the final release-candidate matrix across every pilot-enabled resource: workflows, runs, approvals, audits, costs, connectors, webhooks, artifacts, holds, templates, and action outcomes. Include browser refresh and copied cURL credentials for the appropriate role. | Every allowed action succeeds only in its tenant/workspace; every denied/cross-tenant action returns the documented result without leaking existence or data; screenshots and runtime records identify role and scope without credentials. |
| 8. Retention, compliance, and artifacts | Retention UI, holds, artifact preview/download, TTL expiry, unauthorized/missing records, and tenant isolation have provider-independent evidence. | Approve the pilot retention schedule and perform a disposable end-to-end hold/release/erasure rehearsal on the final candidate. Confirm which audit and economics records survive by policy. | Held data survives; eligible payloads, artifacts, and checkpoints are erased; audit/economics behavior matches policy; the second tenant is byte-for-byte unaffected; a recovery snapshot exists before destructive testing. |
| 9. Audit and Economics closeout | V3 Workflow 2 and Rightsizing calls reconcile across Audit, local Economics, and Regulus, and the UI restores authoritative sub-cent values. The historical production/synthetic split is documented. | Run the remaining Workflow 1 calls and one post-cutover smoke, then reconcile the complete pilot-tagged window. Resolve or explicitly waive the provider-project usage 403 as an external upper-bound limitation; do not fabricate zero usage. Preserve historical zero-cost negative fixtures as exclusions. | Exactly one canonical cost event exists per non-cache provider call; no lifecycle/runtime projection is double-counted; all reservations are committed, released, or explicitly reconciled; Audit/local Economics/Regulus differ by no more than `max($0.000001, 0.5%)`; value and margin remain zero unless an explicit synthetic valuation is recorded. |
| 10. Browser, accessibility, and UI evidence | All 21 published routes passed the automated visual matrix across four viewports, 200% zoom, Chromium, and WebKit; native Safari checkpoints exist for major surfaces. | Repeat the pilot-critical journeys on the final candidate in Chromium and native Safari, including Studio, Run, Approval, Audit, Economics, Rightsizing, Retention, and Artifacts. Capture only deterministic sanitized screenshots. | No clipped/overlapping controls, page overflow, 4xx/5xx, console error, unhandled rejection, or new axe WCAG 2.2 AA violation; keyboard/focus/modal behavior passes; every checkpoint joins to runtime, audit, and economics evidence. |
| 11. Security and dependency release gate | Secret-rejection and hostile/cross-tenant controls have accepted checkpoints; backend and frontend collision suites passed during the merge. | Upgrade or risk-dispose the current frontend dependency audit findings: 7 high and 2 moderate, including direct Next.js findings. Run the complete release-candidate security matrix, full backend suite, frontend tests/build/typecheck, migration rehearsal, and API/schema drift gates on the immutable candidate. | Zero unexplained critical/high production-exploitable findings, zero skipped required security cases, clean secret scan, complete test record with exit codes, successful migration/rollback rehearsal, and a documented exception owner/expiry for any accepted residual risk. |
| 12. Pilot operations | Persistent services, restart instructions, append-only evidence, and bounded rollback procedures exist. | Assign monitoring/on-call ownership; define alerts for health, queue/lease stalls, unsigned/broken audit, reservations, cost caps, webhook backlog, and storage integrity. Run backup/restore and one controlled incident exercise. | A named owner can detect, diagnose, stop, restore, and roll forward without this chat; recovery meets the frozen objective; the support and escalation runbook is usable by someone other than the implementer. |
| 13. Cohort outcome and closeout | The product has extensive synthetic and operator-driven evidence, but that is not evidence that pilot users achieved their intended outcome. | Run the frozen pilot with the named cohort for the agreed period. Capture adoption, task completion, quality review, latency, spend, support incidents, user feedback, and any overrides or manual workarounds. Reconcile the final window and seal the acceptance ledger and discrepancy register. | Every frozen success threshold is met or explicitly waived by its owner; no unresolved severity-one incident or stop condition remains; all criteria are `pass`, `blocked`, `not_run`, or explicitly out of scope; product, security, operations, and pilot-owner signatories accept residual risks and record a proceed, extend, or stop decision. |

## Immediate execution order

1. Freeze pilot scope, owners, release candidate, budgets, and success metrics.
2. Patch or disposition the dependency findings, run the complete release gates,
   and build an immutable candidate from `main`.
3. Rotate credentials and cut the persistent pilot services over to that
   candidate without deleting external state.
4. Execute Workflow 1 repetitions two and three plus the critical negative
   matrix; stop immediately on any audit, isolation, secret, or cost discrepancy.
5. Run one post-cutover batch, approval/action, loop, retention/erasure, and
   role-isolation smoke; run measured Rightsizing only if it is in pilot scope.
6. Complete native Safari/Chromium inspection, backup/restore, rollback, and the
   controlled incident exercise.
7. Open the pilot only after gates 1–12 pass, run the frozen cohort period, and
   evaluate its predeclared success thresholds rather than substituting internal
   test volume for user outcomes.
8. Reconcile all pilot-tagged calls and seal `manifest.json`, `events.ndjson`,
   `acceptance.json`, screenshots, reports, checksums, discrepancy register, and
   signoff. Only then change the status to `pilot_accepted`.

## Non-negotiable stop conditions

Stop the pilot campaign immediately if any secret or authorization material
appears in evidence; a tenant boundary is crossed; an audit chain is broken or
unsigned; an operation/provider outcome is ambiguous without a retained maximum
reservation; a cost cap is exceeded; Audit, Economics, and Regulus diverge
beyond tolerance; an unintended external side effect occurs; storage integrity
fails; or the final candidate has a critical unmitigated vulnerability.

The failed attempt remains append-only evidence. Resume only in a new evidence
root after direct proof of remediation.

## Explicitly outside this pilot bar

Unless the scope owner adds them before the freeze, the following are not
required to accept a bounded pilot and must not be implied by it:

- every private worker callback or machine-only ingestion endpoint;
- statistical production reliability or a generalized model-quality claim;
- multi-region or cross-host SQLite operation;
- non-repudiation from the local HMAC signer;
- production payment, email, or third-party write integrations; and
- full acceptance of all 141 product-surface criteria.

These exclusions reduce scope; they do not turn untested behavior into a pass.

## Adversarial review

The strongest objection is that calling this a “full successful pilot” could be
heard as production readiness. The present system has extensive functional
evidence, but its persistent local topology, local HMAC custody, shared-provider
usage limitation, one-case Rightsizing result, and incomplete Workflow 1
repetition matrix do not justify that interpretation.

The largest risks are scope creep, reusing a compromised credential, accepting
screenshots without correlated backend evidence, treating historical synthetic
cost as deployment spend, and moving shared SQLite writers into an unverified
locking domain. The simpler and safer option is a supervised, single-host,
two-tenant pilot with advisory Rightsizing and only the three validated workflow
families. Expand integrations or scale only after its closeout is signed.

## Evidence and operator reading path

1. [Full-readiness campaign](FULL_READINESS_CAMPAIGN.md) — authoritative claim
   boundary and V1–V3 paid results.
2. [V3 acceptance profile](accelerated-acceptance-v3.json) — consumed bounded
   call/run/budget contract and deferred original criteria.
3. [Discrepancy register](handoff/discrepancy-register.md) — open, reconciled,
   and superseded evidence conditions.
4. [Execution and rollback](handoff/execution-and-rollback.md) — persistent
   topology, credential installation, restart, and recovery procedures.
5. [Live Studio evaluation guide](../../docs/how-to/live-studio-evaluation.md) —
   budget, signing, evidence, and stop-condition mechanics.
6. [Project model](../../PROJECT_MODEL.md) — architecture, invariants, tests,
   and current operational risks.
