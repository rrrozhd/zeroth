# Zeroth pilot-readiness closure plan

## Bottom line

The current candidate is suitable for a controlled demo, but a full pilot is
**not yet accepted**. The evidence supports
`demo_ready_not_full_campaign_accepted`: technical closure now includes fresh
batch, approval/action, and loop runs; immutable rollback and roll-forward;
eight-route Chromium and native Safari inspection; a complete backend run;
migration rollback; API drift; the complete 109-node release-candidate access
matrix; a fresh disposable destructive-retention run; restart and exact-hash
restore drills; signed audit; authoritative accounting; server-owned artifact
identity configuration; and 84.1% source documentation coverage. Pilot
acceptance remains blocked by human-owned credential rotation, assignment and
signoff of an accountable operations owner, and elapsed real-user cohort
outcomes.

This document defines the smallest credible bar for a successful pilot. It does
not relabel deferred full-product criteria as passes and does not claim
production readiness.

## Post-audit security hardening checkpoint — 2026-08-29

Status: **source verification complete; immutable package/image sealing follows
this commit, with no production-readiness claim**.

The source candidate based on local `main` commit `38e915ba` now changes the
enabled-surface assumptions that older evidence below recorded. Browser API
keys are exchange-only and replaced by short-lived Secure/HttpOnly sessions;
cookie mutations enforce exact Origins, CORS origins are exact, and API
`connect-src` is same-origin. Production requires shared signing material.
GitHub installation claiming is admin-only, and the enabled repository ingress
matrix covers tenant isolation, hostile refs/trees, webhook authentication and
replay, checkout containment, token redaction, and restart recovery. Budget
admission defaults fail closed. Untrusted inline/repository units refuse the
local subprocess backend.

Inline author-defined servers are rejected. Registered discovery/dispatch
preserve the authoring UX and use an operator-owned, digest-pinned Docker
profile with non-root/read-only/no-capability/resource/environment/network
restrictions; without the image setting they refuse before spawn. The host
transport survives only behind a development flag that production rejects.
Gateway-only admission likewise cannot promise internal tool-call governance;
configuration fails if full enforcement is requested there.

Current reviewable evidence includes focused red/green security tests, 164
sandbox/executable-unit tests, a clean 251-test repository/GitHub matrix, 1,110
API-surface/signature contract tests, 395 frontend tests, a successful static
production build and API drift check, zero known npm or Python dependency-audit
findings, and a clean full backend run of 12,215 passed, 8 skipped, and 465
deselected in 907.97 seconds. The PR-critical and release-candidate security
tiers passed 134/134 and 145/145 respectively against disposable
Redis/PostgreSQL dependencies; independent coverage and outcome verification
found no missing, unbound, duplicate, skipped, or failed bound node. A
disposable SQLite database upgraded to migration head 035, downgraded to 034,
and upgraded to 035 again. These are source-tree facts, not immutable promotion
or deployed-environment evidence. The package/image identity seal must be
measured from the resulting clean commit before this checkpoint can replace any
historical final-candidate claim below.

Residual risks: MCP image provenance, scanning, Docker-daemon security, and
external egress policy remain operator responsibilities; a standalone console
must use the provided exact-origin CSP image or equivalently reviewed hosting;
GitHub ingress remains a high-consequence optional surface; and explicit
fail-open budget mode still exists for compatibility. The simpler and safer
pilot configuration leaves MCP network disabled and repository ingress off
unless needed, uses Docker/sidecar for untrusted code, keeps budgets fail
closed, and deploys SDK-level tool governance.

## Pilot finalization checkpoint — 2026-08-28

Evidence root:
`~/.local/share/zeroth/evaluations/evaluation-studio-v1/evidence/pilot-finalization-20260828-1`.
Secret-bearing database snapshots remain outside it under
`state-snapshots/pilot-finalization-20260828-1-retention-post-erasure`.

| Gate | Status | Evidence and boundary |
| --- | --- | --- |
| Resource-level access control | **Pass** | The generated release-candidate catalog contained 109 nodes. All 109 passed against disposable loopback Redis; coverage and outcome verification report no missing, unbound, duplicate, skipped, or failed nodes. The matrix spans application resources, SQL/Redis/artifact/checkpoint persistence, workers/restarts, forged scope, identifier guessing, replay, concurrency, stale credentials, and revocation. |
| Destructive retention | **Pass — disposable tenant** | The authenticated 1440×900 Chromium journey refused erasure of a held run, erased an eligible run and every eligible tenant run, removed their artifacts, retained held content, preserved verifiable signed audit chains, retained policy-defined economics, refreshed durable history, and passed axe WCAG 2.2 AA. No provider call or external action occurred. |
| Restore and incident drill | **Pass — technical rehearsal** | The disposable service outage was detected, restart restored health and the held record, and restoring the external snapshot reproduced database SHA-256 `2180eac0c5e289d9aeeec840c0a0534156ad136ca0bbdb8b3994342151b9d4eb` plus held/erased state. This closes the mechanism, not accountable-owner signoff. |
| Operations ownership | **Blocked — human-owned** | [PILOT_SIGNOFF.md](PILOT_SIGNOFF.md) records the technical rehearsal and leaves the real owner, escalation channel, coverage, witness, and signature explicitly unassigned. Automation cannot create accountability. |
| Pilot cohort | **Blocked — elapsed evidence** | [PILOT_COHORT_SIGNOFF.md](PILOT_COHORT_SIGNOFF.md) defines the required cohort, outcomes, incidents, economics, feedback, and three-owner exit decision. No real-user cohort window has occurred, so no outcome is claimed. |
| Server-owned artifact identity | **Configured, certification still separate** | `scripts/configure_dev_artifact_identity.py` refuses dirty Git, measures the full commit and Docker-owned `sha256` image ID, atomically writes both private process settings, and never accepts them from an API client. A trusted promotion receipt remains independently required for production readiness. |
| Documentation coverage | **Pass** | `interrogate src/zeroth --fail-under 84` reports 84.1%, raised from 80.4% by documenting graph-token contracts, approval persistence, orchestration support, and gateway boundaries without changing exclusions. |
| README and UI evidence | **Pass** | Eight fresh authenticated 1360×860 Chromium screenshots show the actual overview, Studio, MCP-tool workflow, Audit, Economics, Rightsizing, Retention, and Artifacts surfaces. The capture script consumes the key only from an environment variable. |

The finalization does not waive the two remaining human gates. The pilot may be
demonstrated under supervision, but it must not be called accepted until the
owner and cohort records are signed with real identities and observations.

## Final-candidate technical closeout — 2026-08-28

Evidence root:
`~/.local/share/zeroth/evaluations/evaluation-studio-v1/evidence/pilot-closeout-20260828-1`.
Database snapshots are deliberately outside that public evidence root under
`state-snapshots/pilot-closeout-20260828-1`; only their hashes are disclosed.

| Requested closeout | Status | Evidence and exact boundary |
| --- | --- | --- |
| Fresh post-freeze batch | **Pass** | Parent `c4fb535f1924446ab6928daa52977fe5` completed eight ordered, isolated children with concurrency four, signed audit, and `$0` provider spend. |
| Fresh approval/action | **Pass** | Run `fa4494dbc7574a6387aaf2b0c81a39ce` completed after approval `b8b2a414a6e2403a9b9211435523e21a` with exactly one durable local-sink marker, one action audit, a valid signed chain, and no external action API. |
| Fresh loop | **Pass** | Run `6b932602311e44ed9f316b16b7c46d75` exercised Repeat then Done at `max_retries=2`, used two retries, and completed with a valid signed chain and `$0` provider spend. |
| Immutable rollback / roll-forward | **Pass** | Deployment version 6 served graph `@5`, then version 7 served graph `@6`; service restarts preserved the fresh run, signed audit, action marker, and complete version history. |
| Eight-route Chromium | **Pass** | Studio, Runs, Approvals, Audit, Economics, Rightsizing, Retention, and Artifacts rendered at 1440×900 with no horizontal overflow or console error; Audit was recaptured after data load. |
| Eight-route native Safari | **Pass** | The same routes rendered through actual macOS Safari at 1216×768. Audit displayed `chain intact · signatures valid`; Economics displayed `$0.0056` actual spend and a `$0.25` fail-closed ceiling. No route produced a loading terminal state or HTTP error. |
| Full backend suite from zero | **Pass** | Final-candidate result: `12119 passed, 8 skipped, 465 deselected` in 889.08 seconds. The eight skips are optional-environment cases; the mandatory release security matrix separately passed 109/109 with zero skips. The audit compatibility correction also has 61 focused passing audit/retention/API tests. |
| Migration rollback | **Pass** | A disposable SQLite database upgraded to head 035, downgraded to 034, and upgraded to 035 again with the expected table restored. Production data was not used. |
| API/schema drift | **Pass** | `npm run check:api` completed with exit code 0 against the committed contract fixtures. |
| Credential custody | **Blocked — human/external** | The provider credential exposed in chat cannot qualify as a pilot credential. A human owner must revoke it, install a new value outside Git, and prove old-value rejection without disclosing either value. |
| Authorization and tenant isolation | **Partial** | Live operator/reviewer/admin identities resolve only in their tenant, and crossed admin credentials return scope-hiding 404s. The full final-candidate resource-by-resource browser matrix remains broader than this accelerated closeout. |
| Retention and compliance | **Partial** | Current Safari and Chromium views show persisted policy, legal hold, prior erasure activity, and correct tenant scope. A new destructive rehearsal was intentionally not run against the retained campaign tenant; it requires a newly disposable fixture and recovery snapshot. |
| Accounting | **Pass for the current campaign window** | Authoritative production spend is `$0.00556027` (`$0` measured, `$0.00556027` estimated), with zero active or ambiguous exposure. The synthetic `$0.01` control proof is explicitly excluded from provider spend and deployment attribution. |
| Signed audit continuity | **Pass after compatibility remediation** | A historical v3 chain initially failed because adding nullable `campaign_id` changed the deserialized digest layout. A test-first compatibility rule now accepts only the historical absent-key form when campaign correlation is null. No audit row was rewritten. The six-record historical chain, four-record fresh chain, and all 170 records attributed to the served deployment verify with valid signatures. |
| Operations owner / recovery | **Blocked — human-owned** | No named on-call owner has yet performed the restore and incident drill independently of the implementer. |
| Cohort outcome | **Blocked — elapsed evidence** | No real pilot cohort period, adoption result, task-success review, support record, or owner signoff exists yet. Synthetic and operator testing cannot substitute for this gate. |

Two repository-level release conditions also remain visible rather than waived:
`/health` reports `production_ready=false` because
`serving_artifact_identity_unavailable` requires a server-owned commit and image
digest, and the documentation-coverage gate reports 80.4% against an 84%
threshold. Neither affects the bounded local demo claim; both block a
production-ready claim.

## Accelerated closure checkpoint — 2026-08-28

The accelerated pass deliberately targeted six technical areas without
relabeling owner, destructive-recovery, or cohort requirements as complete.
One gate is now closed for the bounded pilot configuration, four have additional
candidate evidence but remain partial, and one remains blocked by the release
matrix contract.

| Gate | Status | Candidate evidence | Exact remainder |
| --- | --- | --- | --- |
| 1. Release candidate and runtime cutover | **Partial** | The persistent frontend, primary backend, twin backend, Chroma, and Redis are healthy from `/Users/dondoe/coding/zeroth-release`; the backends mount the reviewed checkout and retain external state. SQLite upgraded to schema head `035` and survived service recreation. | Record immutable image/config/database hashes and execute one sealed rollback/roll-forward with state checks. |
| 4. Workflow 2 composed execution | **Partial** | The sealed V3 bundle still proves three eight-item repetitions, concurrency four, ordered isolated children, signed chains, and reconciled economics; the final-candidate service can still enumerate the pinned parent/child deployments. | Submit and screenshot one new representative eight-item parent after the immutable candidate is frozen, including the selected failure mode and refresh restoration. |
| 5. Governed actions and loops | **Partial** | The persistent state retains the approval/action and Repeat/Done/Limit bundles plus the pilot-visible loop deployments. The current candidate retains the dedicated Loop UI and max-retry contract. | Execute one new approval marker and one new loop boundary against the frozen immutable candidate. Do not call a production action endpoint. |
| 6. Rightsizing decision quality | **Pass — advisory pilot mode** | Revision `e5c76f39` adds a stable `rightsizing.mode.advisory` notice stating that recommendations never change a deployed model automatically. The component regression test passed and the running interface displayed the notice with zero console errors. No apply/deploy action exists on the surface. | A measured `min_cases >= 5` study remains required before any production model-switch decision; it is not required for this advisory-only pilot. |
| 10. Browser, accessibility, and UI evidence | **Partial** | The final candidate rendered the advisory Rightsizing checkpoint through the real local interface with the evidence ID visible and no browser console errors. Existing sealed Chromium/WebKit/native-Safari route bundles remain valid historical evidence. | Repeat all eight pilot-critical routes in Chromium and native Safari after immutable freeze; rerun axe, keyboard/focus, and responsive checkpoints. |
| 11. Security and dependency release gate | **Partial** | `npm audit --json` reports 0 vulnerabilities after upgrading Next.js to 16.3.3 and vulnerable transitive parsers. All 379 frontend tests, TypeScript, and the production build pass. The 96-case PR-critical security tier passes. Mandatory PostgreSQL tests no longer carry a forbidden skip escape hatch; with disposable loopback Redis, the full release-candidate matrix passes 109/109 and both exact coverage and outcome verification pass. The evidence root passes the recursive secret scan. A broad backend run reached 8,520 passes before interruption; its three failures shared one cause—an untracked empty retired-package directory—which was removed, and all 36 affected architecture/wheel tests now pass. | Rerun the full backend suite from zero, then complete migration rollback rehearsal and API/schema drift gates on the immutable candidate. |

This checkpoint is not “half the pilot accepted.” It is the fastest defensible
technical reduction of the remaining work. Gates 2, 3, 7, 8, 9, 12, and 13
remain unchanged because they require credential ownership, new paid live runs,
the full role matrix, destructive retention rehearsal, final accounting,
operations ownership, or elapsed cohort evidence.

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
| 6. Rightsizing decision quality | One measured case completed four calls, persisted after refresh, reconciled to `$0.00051700`, and correctly returned `flagged`; it proves plumbing, not model equivalence. Revision `e5c76f39` now makes the pilot mode visibly advisory and exposes no automatic model-switch action. | Keep Rightsizing advisory for this pilot. A later production-switch decision still requires at least the configured `min_cases=5` representative cases, an approved call budget, a quality rubric, and owner review. | **Passed for advisory pilot scope:** the UI visibly states that it never changes a deployed model automatically. One case remains a flagged lead and must never be presented as a production switch decision. |
| 7. Tenant, role, and data isolation | Twin tenants and operator/reviewer/admin/platform-admin routes have same-tenant and cross-tenant evidence, including scope-hiding 404 behavior. | Execute the final release-candidate matrix across every pilot-enabled resource: workflows, runs, approvals, audits, costs, connectors, webhooks, artifacts, holds, templates, and action outcomes. Include browser refresh and copied cURL credentials for the appropriate role. | Every allowed action succeeds only in its tenant/workspace; every denied/cross-tenant action returns the documented result without leaking existence or data; screenshots and runtime records identify role and scope without credentials. |
| 8. Retention, compliance, and artifacts | Retention UI, holds, artifact preview/download, TTL expiry, unauthorized/missing records, and tenant isolation have provider-independent evidence. | Approve the pilot retention schedule and perform a disposable end-to-end hold/release/erasure rehearsal on the final candidate. Confirm which audit and economics records survive by policy. | Held data survives; eligible payloads, artifacts, and checkpoints are erased; audit/economics behavior matches policy; the second tenant is byte-for-byte unaffected; a recovery snapshot exists before destructive testing. |
| 9. Audit and Economics closeout | V3 Workflow 2 and Rightsizing calls reconcile across Audit, local Economics, and Regulus, and the UI restores authoritative sub-cent values. The historical production/synthetic split is documented. | Run the remaining Workflow 1 calls and one post-cutover smoke, then reconcile the complete pilot-tagged window. Resolve or explicitly waive the provider-project usage 403 as an external upper-bound limitation; do not fabricate zero usage. Preserve historical zero-cost negative fixtures as exclusions. | Exactly one canonical cost event exists per non-cache provider call; no lifecycle/runtime projection is double-counted; all reservations are committed, released, or explicitly reconciled; Audit/local Economics/Regulus differ by no more than `max($0.000001, 0.5%)`; value and margin remain zero unless an explicit synthetic valuation is recorded. |
| 10. Browser, accessibility, and UI evidence | All 21 published routes passed the automated visual matrix across four viewports, 200% zoom, Chromium, and WebKit; native Safari checkpoints exist for major surfaces. | Repeat the pilot-critical journeys on the final candidate in Chromium and native Safari, including Studio, Run, Approval, Audit, Economics, Rightsizing, Retention, and Artifacts. Capture only deterministic sanitized screenshots. | No clipped/overlapping controls, page overflow, 4xx/5xx, console error, unhandled rejection, or new axe WCAG 2.2 AA violation; keyboard/focus/modal behavior passes; every checkpoint joins to runtime, audit, and economics evidence. |
| 11. Security and dependency release gate | Secret-rejection and hostile/cross-tenant controls have accepted checkpoints. Revision `e5c76f39` clears the prior 7 high and 2 moderate frontend audit findings; 379 frontend tests, TypeScript, production build, the 96-case PR-critical tier, and the full 109-case release-candidate security matrix pass. Exact coverage/outcome verification and the evidence secret scan pass. | Finish the full backend suite, migration/rollback rehearsal, and API/schema drift gates on the immutable candidate. | Zero unexplained critical/high production-exploitable findings, zero skipped required security cases, clean secret scan, complete test record with exit codes, successful migration/rollback rehearsal, and a documented exception owner/expiry for any accepted residual risk. |
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
