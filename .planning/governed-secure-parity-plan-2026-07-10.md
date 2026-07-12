# Build plan — get *governed* and *secure* on par with the product statement

*2026-07-10. Companion to `product-assessment-2026-07-10.md`. **Decision (user):
full build to claim** — every governance/security claim made literally true in
code this cycle, not softened. ~FTE-quarter effort (~3 months). Every item below
maps to a specific claim in `README.md` / `.planning/PROJECT.md` and cites the
current code truth. Feed into GSD as a milestone once approved.*

## Scope

**In:** every place the product statement claims a *governance* or *security*
property the code doesn't fully deliver. **Out (not a governance/security claim —
tracked in `product-gaps-2026-07-07.md`, not here):** triggers/schedules,
streaming, SAML/custom roles, published SDKs, the econ *value*-axis work. **Budget
caps are IN** — the README explicitly claims "per-tenant spend caps enforced"
(a governance control claim), and it's currently false.

## Claim ↔ code inventory (verified 2026-07-10, v0.10 working tree)

| # | Claim (source) | Current truth | Verdict |
|---|---|---|---|
| C1 | "per-tenant spend caps **enforced** through the bundled economic control plane" (README:128,170) | `BudgetEnforcer` reads `/dashboard/kpis`, which returns `total_ai_spend_usd`/`net_ai_margin_usd` — **not** the `total_cost_usd`/`budget_cap_usd` it parses (`econ/budget.py:76-78`). Spend→0, cap→None, **never trips**. The `enforcement/` module holds exactly those fields (`econ_plane/enforcement/service.py:223-224`) on an endpoint the enforcer never calls. Wire crossed. | **FALSE** |
| C2 | "**tenant isolation**" (PROJECT.md); "per-tenant" memory/budgets (README) | Memory connectors have **zero** tenant namespacing → SHARED-scope memory is cross-tenant readable (`core/memory/*.py`). Graph/deployment models have no tenant column. Audits are now tenant-true (`runtime.py:1311`) — the one piece that is. | **FALSE at data layer** |
| C3 | "capability-based rules controlling what **agents** can do (network, file, memory, secrets)" (README:126) | Docker/sidecar sandbox **does** enforce: network egress denied when off (`runner.py:654-657`), read-only root + caps dropped (`sandbox.py:135`), secret env filtered by `SECRET_ACCESS` (`runner.py:588-599`). But default **LOCAL backend does not enforce network** (`sandbox.py:760-762`), **agents/tools get no behavioral interception** (`guard.evaluate` checks self-declared bindings only), and per-connector `MEMORY_READ` is audited, not enforced. | **PARTIAL** (over-reaches on "agents" + default backend) |
| C4 | "human-in-the-loop gates"; "Approval SLA timeouts with escalation" | Notification is outbound HMAC **webhook only** (`approvals/service.py`, `service/webhook_api.py`); no email/Slack. Escalation = timeout state change. Silent-stuck failure mode. | **GAP** |
| C5 | "**deployment provenance**"; "evidence summaries"; attestation | All digests are `hashlib.sha256` (`provenance.py:61,107`); `verify_attestation` recomputes (`:66`). Tamper-**evident** vs accident, not **signed** vs a malicious writer. No chain-verify API (test-only). | **WEAK** |
| C6 | governance/audit for the compliance buyer | **No retention/purge** for audits/runs/cost (only redis cache TTL). Unbounded growth + GDPR data-minimization contradiction. Audit chain is append-only hash-chained (naive delete breaks verification). | **GAP** |
| C7 | "every executable unit runs inside a governed sandbox" | README **already softened** (`:10`, `:282` — "can run inside… default local is for development"). | **AT PARITY** ✓ |
| C8 | Policy "secret usage"; per-tenant | LLM keys are **process-global env vars**; no per-tenant/per-deployment keys, no vault/KMS. | **GAP** |

## Workstreams (each = build the claim true)

### WS-A — Budget caps actually trip *(claim C1)*  · effort **S–M**
- **Do:** point `BudgetEnforcer` at the `enforcement/` endpoint that returns
  `total_cost_usd`+`budget_cap_usd` (or extend `/dashboard/kpis` to include them);
  wire the two live enforcement points (pre-LLM-call `runner.py`, pre-fan-out
  `runtime.py`) to the real cap; add a per-run/per-request ceiling; make fail-open
  vs fail-closed configurable (keep fail-open default, document).
- **Accept:** seed `upsert_tenant_budget(tenant, $X)`; a run exceeding `$X` **halts**
  against the **bundled** econ_plane (test proves it); README:128/170 becomes true.
- **Deps:** none — runs already carry `run.tenant_id`.

### WS-B — Tenant isolation, end to end *(claim C2 — the big rock)*  · effort **L**
- **Do:** (1) thread `tenant_id` through `MemoryScope` resolution and **namespace
  every connector key** (redis_kv, redis_thread, pgvector, chroma, elastic) —
  SHARED becomes *shared-within-tenant*; (2) add `tenant_id` column + Alembic
  migration to graphs and deployments; (3) tenant filter at the **repository**
  layer (list/get), not just the API; (4) tenant guard at the **connector** layer;
  (5) backfill migration for existing rows (default tenant); (6) a **cross-tenant
  leakage test matrix** — write as A, read as B → denied — across every connector
  and repo.
- **Accept:** SHARED write under tenant A is unreadable by tenant B on all
  connectors; graph/deployment lists are tenant-scoped; leak-matrix green;
  PROJECT.md "tenant isolation" is true.
- **Deps:** migration hygiene (watch DuckDB `Decimal(str(x))`, ` 2.`-dupes per
  CLAUDE.md). Foundational — WS-E/F build on per-tenant.

### WS-C — Behavioral capability enforcement for agents *(claim C3)*  · effort **M–L**
- **Do:** (1) gate agent **tool dispatch** by declared capability — an undeclared
  tool/network use is denied, not just unaudited; (2) enforce `MEMORY_READ`/
  `MEMORY_WRITE` at `MemoryConnectorResolver` **before** the connector call, not
  only via audit; (3) resolve the LOCAL-backend egress gap — either enforce egress
  or hard-require Docker/sidecar for NETWORK-bearing untrusted nodes (and keep the
  README caveat precise).
- **Accept:** an agent tool call lacking its declared capability is denied; a
  memory read without `MEMORY_READ` is denied; an undeclared network call from a
  unit is blocked on the enforcing backend; README:126 true **including agents**.
- **Deps:** memory gate interacts with WS-B (do after B's connector layer exists).

### WS-D — Provenance: tamper-evident → signed + verifiable *(claim C5)*  · effort **M + S**
- **Do:** introduce a **pluggable signing-key provider** (env for dev, **KMS/vault
  for prod** — shared with WS-F) with a **documented trust model** (where the key
  lives, rotation, what "signed" asserts); sign the attestation payload and the
  audit-chain head; expose `POST /runs/{id}/verify-chain` and
  `GET /deployments/{ref}/attestation/verify`; add a console "verified ✓" badge.
- **Accept:** tampering any record fails verification; the signature verifies with
  the public key / KMS; verify endpoints live; the trust model is written down
  (no "signed" claim stronger than the key custody supports).
- **Deps:** WS-F (key provider). *Advisor note:* don't let "signed" imply PKI if
  the key sits in process env — the value of C5 is bounded by C8/WS-F.

### WS-E — Retention that survives the immutable chain *(claim C6)*  · effort **M–L**
- **Do:** per-tenant configurable TTL + purge worker using **crypto-erasure**
  (delete plaintext, keep the hash) or **tombstoning** — *not* row deletion, which
  would break chain verification; an **evidence-bundle legal-hold** exemption;
  documented GDPR right-to-erasure posture (erasure via crypto-erasure).
- **Accept:** records past TTL are crypto-erased/tombstoned; the audit chain
  **still verifies** over tombstones; legal-held records are exempt; a
  right-to-erasure request removes tenant PII without breaking audit integrity.
- **Deps:** WS-D (chain-verify must survive purge — design them together) + WS-B
  (per-tenant retention). *Advisor-flagged collision — reconciled here by design.*

### WS-F — Secret & key isolation *(claim C8; backs WS-D)*  · effort **M**
- **Do:** pluggable secret provider (env dev / vault/KMS prod); per-tenant/
  per-deployment LLM key resolution at adapter build; migrate the WS-D signing key
  and LLM keys onto it.
- **Accept:** tenant A's LLM key is never visible to tenant B's run; prod keys
  resolve from vault; the signing key lives in the provider, not process env.
- **Deps:** WS-B (per-tenant). Enables WS-D's real trust model.

### WS-0 — Interim honesty *(runs alongside; days)*
While B/C/D/E/F are in flight for weeks, the README/PROJECT **cannot keep implying
they're done**. Caveat the not-yet-true claims (tenant isolation, agent capability
control, signed provenance, retention) now; **restore each claim's full-strength
wording as its workstream lands**. For a governance vendor, honesty at every
intermediate state is the whole ballgame — this is non-negotiable even in full-build.

## Phase sequence (dependency- and leverage-ordered)

| Phase | Contents | Why here | Rough |
|---|---|---|---|
| **0** | WS-0 interim honesty | Honest while building; hours | days |
| **1** | WS-A budget wire · WS-D verify-API (read half) | Smallest surface, highest headline credibility | ~2 wk |
| **2** | WS-B tenant isolation | Foundational; E/F depend on per-tenant | ~4–6 wk |
| **3** | WS-C capability enforcement · WS-F secret/key isolation | C needs B's connector layer; F enables D | ~3–4 wk |
| **4** | WS-D signing + trust model · WS-E retention | D needs F's key provider; E needs D chain-verify + B | ~3–4 wk |
| **—** | Restore each claim's wording as its WS lands (WS-0 closure) | Parity is only real when the claim is true *and* stated | ongoing |

## Risks / watch-items

- **Migrations (WS-B):** tenant columns + backfill on graphs/deployments/memory —
  Alembic care; DuckDB `Decimal(str(x))`; clean ` 2.`-suffixed dupes first.
- **Retention × chain (WS-E):** the one true design collision — solved by
  crypto-erasure/tombstoning + legal-hold, *not* delete. Design WS-D and WS-E in
  the same pass so verify-chain accounts for tombstones.
- **"Signed" vs key custody (WS-D/F):** don't claim PKI-grade signing while the key
  is env-resident. WS-F must land the vault path for WS-D's claim to be honest.
- **Full-build is demand-ungated:** the assessment's #1 gap (no proof workload, solo
  maintainer, GTM undecided) is unaddressed by this plan. Recommend running **one
  real proof workload in parallel** so the ~3 months of hardening lands on a
  validated wedge, not ahead of one.

## Next step

Formalize as a GSD milestone (`/gsd-new-milestone` → "Governance & Security
Parity") with Phases 1–4 above, or `/gsd-add-phase` each workstream. WS-A + WS-D
(verify-API) are the fastest credibility wins and have no dependencies — good
Phase 1 to start.
