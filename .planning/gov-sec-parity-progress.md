# Governed + Secure parity build — execution progress

*Started 2026-07-11. Executing the "full build to claim" pass (user: "do it in a pass",
NOT a GSD milestone). Plan: `governed-secure-parity-plan-2026-07-10.md`. Code-grounded
spec + reconciliation (the source of truth for edit sites, per WS): the workflow output
at `.../tasks/wa0y77twz.output` (7-agent spec pass, HEAD f284dc9). This file = running status.*

## Decisions locked (user, 2026-07-11)
- **Tenancy model = SINGLE-TENANT PER DEPLOYMENT.** One service = one deployment = one
  tenant. WS-B (tenant isolation) is therefore **defense-in-depth hardening**, not
  multi-tenant-SaaS breach-prevention — still build memory namespacing + repo filters, but
  the SHARED-memory leak only bites if an operator points multiple tenant services at one
  physical backend. Per-tenant key resolution (WS-F) keys off the single `deployment.tenant_id`.
- **Capability rollout = ENFORCE IMMEDIATELY (fail-closed).** WS-C wires the default
  PolicyGuard and flips on now; **must backfill `capability_bindings` on demo/reference
  graphs (apps/vendor_dd, demos/) in the same pass** so existing graphs don't break.

## Build order (REVISED after single-tenant decision): A → F → D → (B+C together) → E
Single-tenant lowered WS-B's urgency (hardening), and WS-F unblocked WS-D, so WS-D moved
ahead of WS-B. B+C are done together (they co-edit `memory/registry.py` — one coherent pass).
Migrations serialize as ONE linear Alembic chain off head **005**, assigned by ACTUAL build
order: **`006`=WS-D** (deployment_versions signature cols), **`007`=WS-B** (graph_versions +
memory_connector_configs tenant), **`008`=WS-E** (retention tables). WS-A, WS-C, WS-F add none.
`node_audits` NEVER gets a column ADD — new NodeAuditRecord fields serialize into `record_json`.
**Greenfield RESOLVED:** `bootstrap.run_migrations` runs `alembic upgrade head` — Alembic is the
SOLE app-schema path (only raw CREATE is Alembic's own `schema_versions` tracker). New columns go
ONLY in the new migration file; do NOT patch `001`.

## Status per workstream
| WS | Title | Status | Notes |
|----|-------|--------|-------|
| **A** | Budget caps trip vs bundled plane | ✅ **DONE + verified** | uncommitted; 1652 pass |
| **F** | Secret & key isolation | ✅ **DONE + verified** | uncommitted; full suite 1677 pass; Vault unit-only (no live server) |
| **D** | Signed provenance | ✅ **DONE + verified** | uncommitted; full suite 1705 pass; migration **006**; env-HMAC + local Ed25519 fully tested (KMS deferred); 3-state badge; `_DIGEST_EXCLUDED_FIELDS` seam for WS-E |
| **B** | Tenant isolation (hardening) | ✅ **DONE + verified** | uncommitted; full suite 1741 pass; migration **007**; `Scoped(TenantScoped(Auditing(raw)))`; leak matrix green; left `effective_capabilities` kwarg for C |
| **C** | Capability enforcement | ✅ **DONE + verified** | uncommitted; full suite 1776 pass; enforce-by-default fail-closed; executor-not-invoked-on-denial tested; backfilled vendor_dd + examples/05 + 1 fixture; governai no double-enforce; mcp://→EXTERNAL_API_CALL |
| **E** | Retention/erasure (GDPR) | ✅ **DONE + verified** | uncommitted; full suite **1801** pass; migration **008**; signed chain STILL verifies after crypto-erasing a record (definitive); legal-hold beats TTL+RTE; econ-erase unwired-by-default + documented |

**ALL 6 BUILT + verified (uncommitted). Full suite 1806 pass.**

## Holistic adversarial refutation (4 skeptics + synth) — verdict was parity-NOT-achieved; gaps fixed
Full findings: `tasks/wp9ow8xdo.output`. Real gaps found (suite had missed all):
- **G3 (critical)** — signed v2 audit records: verify folded STORED pii_commitments, never recomputed from live PII → a non-erased record's PII could be tampered with digest+signature still verifying. **FIXED** (verifier.py: non-erased recomputes live commitments, erased trusts stored; shared `_compute_pii_commitments`; tamper-detection test). Kept erased-still-verifies green.
- **G2 (high)** — capability enforce-by-default DENIED correctly-declared memory/tool ops on fan-out BRANCH nodes (`_enforce_policy_for_branch` never persisted the granted set). **FIXED** (runtime.py:1187 persists via setdefault; branch-allow + branch-deny tests in tests/parallel/).
- **G4 (medium)** — `GET /deployments` listed across tenants (endpoint didn't pass tenant_id). **FIXED** (deployment_api.py:80).
- **G9 (low)** — connector test-probe bypassed the tenant/capability wrappers. **FIXED** (routes through resolver).
- **G1 (high)** — per-tenant caps OFF by default (`regulus.enabled=False`); README claimed default-on. **User chose DEFAULT-ENABLE.** 🔵 building: flip default True + auto-generate a secure ephemeral ECP_JWT_SECRET when placeholder (preserves token unforgeability, lets fresh deploys boot) + README/SECURITY update + cap-trips-by-default test.
- Not gaps (verified false-alarm / known-deferred): signing-off-by-default (honest doc), budget fail-open default (documented), self-declared caps (design), repo-filter opt-in (defense-in-depth per single-tenant), 'default' sentinel shared-bucket (doc note).
Fixer teeth-verified each (reverted fix → test fails). Full suite 1806 pass.

## ✅ PARITY ACHIEVED (2026-07-12) — all gaps fixed + verified
G1 closed: `regulus.enabled` default→True; app.py auto-generates a strong ephemeral ECP_JWT_SECRET
when placeholder (unforgeability preserved) so default deploys boot; README/SECURITY updated;
`test_cap_trips_by_default_no_env_flags` proves caps trip with ZERO env flags. **Full suite 1808 pass.**
G2/G3/G4/G9 fixed + teeth-verified. All 6 workstreams + all 5 refutation gaps done, uncommitted.

Honest residual caveats (documented, not gaps): Vault/KMS unit-tested only (no live infra);
multi-worker needs explicit ECP_JWT_SECRET (ephemeral is per-process); in-process budget default
needs the `regulus` extra (in `[all]`); econ-event erasure unwired-by-default; repo-layer tenant
filters are defense-in-depth (single-tenant model); 'default' sentinel = shared bucket (doc note).

## COMMIT (needs explicit user authorization — NOT started)
Working tree = clean hardening pass on HEAD f284dc9 (v0.8); 60 files, +2738/-300; version NOT bumped.
This is a HIGH-tier change (new subsystems: signing, retention, tenant-scope, capability enforcement,
secret provider) → **0.8 → 0.9**. Changes are interleaved in shared hot files, so propose a small set
of thematic commits (not 6 perfectly-separated ones). Watch CI after any authorized push.

## WS-A — DONE (uncommitted, 2026-07-11)
Real gap was small (endpoint/auth/halt-points already existed at f284dc9). Shipped:
- **Linchpin:** `BudgetEnforcer` gained `asgi_app` param → `httpx.ASGITransport` to the
  in-process `/regulus` mount (base_url `http://regulus.internal/v1`); `bootstrap.py`
  imports `zeroth.econ_plane.main.app` and passes it (guarded), base_url nulled. Was silently
  fail-opening to `localhost:8000`. External-HTTP path unchanged when `asgi_app is None`.
- **fail_closed** knob (`ZEROTH_REGULUS__FAIL_CLOSED`, default open): error path denies
  `(False,0.0,0.0)`+WARNING; never caches on error; 200-null-cap stays unlimited.
- **Per-run ceiling** (`ZEROTH_REGULUS__PER_RUN_CAP_USD`): pre-dispatch
  `_sum_run_cost(run) >= cap` → `BudgetExceededError` → `_fail_run`; works with regulus off.
- **Spec-error caught & fixed:** `_sum_run_cost` did NOT actually sum for sequential runs —
  `RunHistoryEntry` lacked a cost field (`extra="forbid"`). Added optional
  `RunHistoryEntry.cost_usd`, populated in `_record_history`. Without this the per-run cap
  would silently never trip.
- Files: `econ/models.py`, `econ/budget.py`, `orchestrator/runtime.py`, `runs/models.py`,
  `service/bootstrap.py`, `README.md`, tests in `test_regulus_mount.py`/`test_econ_budget.py`/
  `tests/orchestrator/test_per_run_cap.py`.
- **Verified:** 25 targeted tests + full suite **1652 passed / 0 failed**, ruff clean.
  Linchpin wiring confirmed (bootstrap hands the sub-app that serves `/v1`; parent lifespan
  bootstraps the mounted plane's schema). Versioning: NOT bumped (uncommitted) — Med-tier when committed.

## Cross-WS constraints (from reconciliation — obey these)
- **`bootstrap.py`** is edited by every WS on the same constructor calls — apply as strictly
  additive hunks in order A→F→B→D→C→E; never two WS in one patch cycle. (A done; F in flight.)
- **`audit/verifier.py` `_compute_record_digest`** is co-owned by WS-D + WS-E — the single
  merged digest must: null `record_digest`, EXCLUDE signature fields (WS-D) AND
  erasure-metadata fields (WS-E), and for `digest_version>=2` substitute `pii_commitments`
  for plaintext. WS-D authors the base contract first; WS-E extends. Hard blocker for WS-E.
- **`memory/registry.py` resolve()`** gains TWO per-call params + TWO nested wrappers, authored
  in ONE pass (WS-B TenantScoped + WS-C CapabilityEnforcing): outermost-first
  `Capability(TenantScoped(Scoped(Auditing(raw))))`. Resolver stays a shared singleton — tenant/caps are per-call, never on `__init__`.
- **WS-D signing key MUST come from WS-F's SecretProvider** (`resolve_secret('signing.deployment')`) —
  no second key source. Env-HMAC path is key-custody-bounded (NOT PKI/non-repudiation); the
  strong "signed vs malicious writer" claim is gated on WS-F's vault path.
- **WS-B trio defaults adopted** (recommended): guard REJECTS empty/missing tenant (fail-closed,
  raises) but PERMITS explicit `'default'` as a reserved single-tenant sentinel; **global
  graph_id** (simple ADD COLUMN, no PK rebuild); **explicit tenant_id param** threaded to repo
  get/list (no contextvar).
- **Pin `governai==0.2.3`** + add a contract test — WS-B wraps below / WS-C around its
  external `ScopedMemoryConnector`; a bump changing `_resolve_target`'s `__shared__` breaks isolation.

## Guardrails for the whole pass
No commits / pushes without explicit user authorization. Each WS: implement → run tests →
adversarially verify → keep uncommitted for review. Version bump is Med/High at commit time.
