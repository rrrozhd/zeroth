# Zeroth at v0.10 — how useful, and what's missing to fulfil the mission

*2026-07-10 — consolidated current-state assessment against the mission:
**economically viable, governed, secure AI apps**. Rolls the three overlapping
strategic docs up to today's code (v0.10) so they stop fragmenting: supersedes the
stale `v0.5.x` status header on `product-gaps-2026-07-07.md`, folds in
`econ-viability-assessment-2026-07-10.md` (econ lens) and the 2026-07-06 positioning
audit (security lens). Every claim below re-verified against current source.*

## Bottom line — both halves of the question

**How useful:** the runtime is real and further along than any of the prior audits'
headers imply. The three pillars are **not equally mature** — and that gradient *is*
the usefulness answer:

| Pillar | Maturity | One-line verdict |
|---|---|---|
| **Economically viable** | **Strongest** | On-by-default cost metering + unit economics + an incumbent-as-reference right-sizing method a gateway *structurally cannot* replicate. Now even acts (opt-in cost cascade), not just advises. |
| **Governed** | **Mid** | The architectural differentiator — hash-chained audits, first-class approval nodes, evidence bundles, deployment attestation. The canvas→run loop now largely closes; tenant-true audits fixed. But approvals can't reach a human, there's no retention, and no triggers. |
| **Secure** | **Weakest — relative to its own README** | econ-plane auth holes are closed, but the default sandbox is a plain subprocess, SHARED memory is cross-tenant readable, and capabilities are self-declared. A skeptical CISO's first hour finds the pitch outrunning the code. |

**What's missing (the honest #1 is not code):** no design partner, no proof workload,
zero demand evidence for the runtime-vs-gateway thesis, and single-maintainer vendor
viability — which *is* the procurement diligence the security pillar is meant to pass.
For a *mission-fulfilment* question this outranks any individual feature. The code gaps
below are real; this one gates whether closing them matters.

---

## Pillar 1 — Economically viable (strongest)

**Genuinely useful, and hard to copy:**
- **Cost metering is now on by default** (v0.8). `CostEstimator` (local litellm pricing,
  no backend) is always built; `cost_usd` and every local lens populate out of the box.
  Regulus is now optional (event stream + budget caps only). *This was the #0 blocker — closed.*
- **A value axis exists** — `econ/unit_economics.py`: cost-per-**successful**-outcome,
  loading the failure tax onto each good result; `by_workflow` / `by_tenant` breakdowns;
  quality-aware overlay (`econ/quality.py`, cost-per-*good* over a labelled subset, honest
  coverage floor). This is the literal answer to "economically viable = value ÷ cost," and
  it's the one thing a proxy-layer competitor can't do — a gateway sees API traffic, not
  whether the run succeeded at its business task.
- **Structural waste attribution** (`econ/waste.py` + `/v1/econ/waste` + UI): cost-per-node,
  "paid for a failed run," loop/retry waste — requires being the runtime.
- **Right-sizing by incumbent-as-reference**: "does the cheap model match the model you
  already trust, on your own real traffic?" — zero labelling, uncopyable at the gateway.
- **It now acts, not just advises** — `CascadingProviderAdapter` (`agent_runtime/cascade.py`)
  is wired into dispatch (`runtime.py:1324`): cheap-first, escalate to the node's
  `model_provider` on failure/low confidence. The "advises but never routes" gap is
  *partially* closed.

**Still missing:**
- **Budget caps still can't trip against the bundled backend — the #1 headline claim is
  still demo-proof only.** Confirmed by reading, not inference: `BudgetEnforcer.check_budget`
  reads `total_cost_usd` / `budget_cap_usd` from `/dashboard/kpis` (`econ/budget.py:76-78`),
  but that endpoint returns `total_ai_spend_usd` / `net_ai_margin_usd` / `efficiency_index`
  and **neither** of those two fields (`econ_plane/dashboard/service.py:13-45`) → `spend`
  defaults to `0`, `cap` to `None`, check never fires. A *new* `enforcement/` module
  (`TenantBudget`, `upsert_tenant_budget`, a service returning both fields —
  `econ_plane/enforcement/service.py:223-224`) holds exactly the missing data but sits on a
  different endpoint the enforcer never calls. **The pieces exist; the wire is crossed.**
  Plus still fail-open by design, 30s-cached, tenant-level only.
- **The cascade is opt-in, low-criticality only** (`cascade_enabled` gate, `runtime.py:1333`)
  — not the general default-safe router that is the biggest live cost lever.
- **Tool-agent replay deferred** — right-sizing's *measured* verdict is tool-free only, so the
  differentiator reaches the cheapest nodes and skips the priciest (tool-using agents).
- **No token-level waste** (prompt bloat, oversized context, over-provisioned `max_tokens`) —
  often the largest real waste; `waste.py` doesn't see it. **No time axis** (no spend-over-time,
  anomaly alerts, forecasting).
- On-by-default *attribution* is unit-verified only; a full orchestrator-run acceptance test
  (deterministic provider → assert persisted `cost_usd`) is the remaining proof.

## Pillar 2 — Governed (the differentiator, mid-maturity)

**Genuinely useful:**
- Hash-chained audit trail, **first-class approval nodes**, policy guard, evidence bundles,
  deployment attestation — governance enforced *inside* the runtime that also deploys the
  service. Architecturally real and, as of today, **unowned in the market**.
- **Audit tenant attribution is fixed** — node audits now stamp `run.tenant_id`
  (`runtime.py:1311`), not a hardcoded `"default"`. For the "whose decision was this" buyer,
  this was critical and it's closed.
- **The canvas→run loop now largely closes** — `publish_workflow` endpoint
  (`studio_api.py:480`), a `deployment_api.py`, and a **real runner factory** that reads the
  canvas `model_provider` (`agent_runtime/factory.py:69-101`) — the field is no longer
  decorative. "Draw it, publish it, run it, governed" is now mostly reachable.

**Still missing / weak:**
- **Capabilities are self-declared** by a node's own `capability_bindings` — a node that
  declares nothing needs no permission; nothing intercepts actual behaviour outside the
  sandbox path. Governance you can opt out of by omission.
- **Approval gates can't reach a human** — notification is outbound HMAC webhook only; no
  email/Slack. The flagship governance feature has a built-in "run silently stuck on approval"
  failure mode unless every customer builds a bridge.
- **No retention/purge** for audits/runs/cost events — unbounded growth, and a direct
  GDPR/data-minimisation contradiction for the *exact* compliance buyer being targeted.
- **No triggers** — the only way to start a run is client `POST /runs`. No cron/schedule, no
  inbound webhook, no queue consumer. "Run this every morning" makes the buyer build a scheduler.
- **No streaming** anywhere (SSE/WebSocket: zero hits) — console polls; reads as prototype in a bake-off.
- Chain-verification API surface is thin (still largely test-only); attestation digests are
  unsigned (recomputable hashes). Remaining loop items: JSON-Schema contracts (A.4) and serving
  hot-swap (A.7 — deploy still binds one deployment per process at restart).

## Pillar 3 — Secure (weakest *relative to its own claims*)

**Fixed (v0.4, published):** econ-plane open token issuer blocked at the gate; fail-closed
startup guard on placeholder JWT secret; RBAC on studio (`WORKFLOW_*`) and cost
(`METRICS_READ`, admin-only) APIs.

**Current gaps that fail a CISO's first hour:**
- **Default sandbox is a plain subprocess** — `SandboxBackendMode.LOCAL`
  (`execution_units/sandbox.py:176,212`); native Python units run in-process; the Docker path
  lacks `--read-only` / `--cap-drop` / seccomp / non-root. The README says "every executable
  unit runs inside a governed sandbox." **Either harden the default or soften the claim** —
  a governance vendor caught overstating is unfundable.
- **Memory has zero tenant namespacing** — no `tenant` reference anywhere in
  `core/memory/*.py`; SHARED-scope memory is **cross-tenant readable**. Multi-tenancy is
  API-layer only; graphs have no tenant column.
- **LLM keys are process-global env vars** — no per-tenant / per-deployment keys, no vault/KMS.
- SSO: OIDC/JWT (incl. JWKS) present; **SAML absent**; roles are a hardcoded 3-member enum with
  no custom-role management — an enterprise gate.

---

## The gap that outranks all code gaps

Both prior audits hammer it and it hasn't moved: **zero demand evidence.** No design partner,
no prospect has asked for runtime-level Art. 12 evidence bundles; the "why the runtime, not the
gateway" thesis is architecturally sound but market-unvalidated. A single-maintainer Apache-2.0
project asking enterprises to hang their regulatory evidence chain on it **fails the same
procurement review the security pillar is built to pass** — vendor viability *is* the diligence.
And the GTM motion is unchosen: self-serve developer (⇒ funnel + loop matter, tenant depth
mostly doesn't) vs sales-led regulated enterprise (⇒ reference architecture + filled security
questionnaire + services matter, nobody pip-installs at minute 10). *Which half of the code
roadmap is noise depends on a decision the code can't make.*

## Recommended order (v0.10)

0. **Truth-in-labeling — hours, still partly outstanding.** Fix the README sandbox claim;
   caveat the budget-cap story until the enforcer is rewired. Overstating governance is fatal.
1. **One proof workload.** A design partner — or the founder's own real workload — running one
   business-shaped governed graph in production. The only thing that de-risks the demand thesis,
   produces the case study, and answers the hardest buyer question.
2. **Make the money pitch true.** Rewire `BudgetEnforcer` to the `enforcement/` endpoint that
   actually stores the cap (not `/dashboard/kpis`); graduate the cascade from opt-in to
   default-safe; close tool-agent replay so right-sizing reaches the priciest nodes.
3. **Close the security gaps that fail a CISO hour.** Sandbox default, memory tenant
   namespacing, real capability enforcement — before any external diligence.
4. **Unblock the funnel and surface the buried moat.** Real CLI/Docker/PyPI; approval
   email/Slack notifier + a schedule trigger + retention policy; the exportable Art. 12
   compliance-pack + signed attestation as the sales artifact.

**Net:** the distinctive core — governance and economics enforced *inside* the runtime that
also deploys the service — is architecturally real and unowned. What it's missing, in order:
one customer who proves the demand thesis; claims that survive a security team (budget wiring,
sandbox default, tenant isolation); the value-axis lever pulled all the way (default cascade +
tool-agent replay); and a written owner-continuity story for the compliance evidence it asks
buyers to depend on.
