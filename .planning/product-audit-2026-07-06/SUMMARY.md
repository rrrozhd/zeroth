# Zeroth — Complete Product Audit (2026-07-06)

Synthesis of three parallel audits run on branch `feat/console-frontend` (working tree).
Full detail: [tech-audit.md](tech-audit.md) · [design-audit.md](design-audit.md) · [market-positioning.md](market-positioning.md)

## Verdict in one paragraph

Zeroth core is unusually mature for its stage (1,440 tests, real migrations, disciplined
redaction, honest deployment docs) and the console chrome is production-grade — but the
product's own differentiator is currently its weakest link: the bundled Regulus econ plane
has two critical auth holes and silently fails open, which means "governed, cost-controlled
agents" is a promise the runtime doesn't yet keep under adversarial conditions. The market
window is real and time-boxed: EU AI Act Annex III obligations (Art. 12 event-level logging,
Art. 14 human oversight) become enforceable **August 2, 2026**, no one owns "governance
enforced by the runtime itself," and the closest competitors are 12–24 months of bolt-on
work away from matching it.

## Positioning (recommended)

> **Zeroth is the governed agent runtime: build multi-agent graphs in code or on canvas and
> deploy them as multi-tenant API services where audit trails, human approval gates, and
> per-tenant budget caps are enforced by the runtime itself — not bolted on afterward.**

- **Wedge #1:** compliance-grade agent runtime for regulated / EU-exposed teams (fintech,
  insurance, healthtech, EU-selling B2B SaaS). Typed per-node audit + approval gates map
  nearly one-to-one onto EU AI Act Art. 12/14.
- **ICP:** buyer = VP Eng / Head of Platform at 200–5,000-person companies; users =
  platform/ML engineers who outgrew raw frameworks but reject no-code. CISO/compliance and
  FinOps are the economic influencers.
- **Market validation for medium-code:** OpenAI is retiring its no-code Agent Builder
  (Nov 30, 2026) in favor of the code-first SDK.
- **GTM:** open-core; Team ~$500–1K/mo with per-run metering; Enterprise $80–150K/yr;
  design-partner-led against the Aug 2026 deadline.
- **Threats:** LangChain/LangSmith (feature superset, could ship a "governance mode");
  ServiceNow/Palo Alto absorbing the governance layer (Traceloop, Portkey acquisitions);
  CrewAI Enterprise (already sells SOC2/HIPAA/on-prem at $60–120K).

## What's missing to be a hit (ranked)

**Credibility blockers (fix before any external exposure):**
1. **econ_plane token mint is open** — `POST /v1/auth/token` issues Admin JWTs to any
   caller, no credential check (`src/econ_plane/auth/api.py:13`). Any role-less API key
   escalates to econ Admin.
2. **JWT secret defaults to `"change-me"`** with no startup guard, shared by Zeroth's own
   service tokens (`src/econ_plane/config.py:6`, `src/zeroth/core/econ/service_auth.py:56`).
3. **Studio + cost APIs have zero RBAC** — any authenticated key can author/archive
   workflows (`src/zeroth/core/service/studio_api.py`, `cost_api.py`).
4. **Budget enforcement fails open silently** — bare `except: return True` with no
   log/metric (`src/zeroth/core/econ/budget.py:78`). A governance product must at minimum
   *observe* its own failure to govern.
5. **Audit hash-chain forks under parallel fan-out** (`src/zeroth/core/audit/repository.py:34`)
   — the tamper-evidence claim breaks exactly when agents parallelize.

**Table stakes the winners have (product gaps):**
6. Evals/testing harness (LangSmith, Braintrust, Vellum all have one — blocks any bake-off).
7. OpenTelemetry/OpenInference trace export (proprietary audit format = silo).
8. Streaming UX (SSE from deployed APIs + console).
9. Prompt/graph versioning with diff + rollback (also strengthens the audit story).
10. SOC2 Type II + SSO/SAML/RBAC (a governance product without SOC2 is self-refuting).

**Console (to look like a paid product, not a polished beta):**
11. Run-centric detail view unifying Timeline/Audit/Approvals/Output — today three
    disconnected pages describe one execution.
12. Designed run timeline (status dot / duration / cost per step) instead of a JSON dump.
13. Cost breakdown per node/run + budget bar — the econ plane has the data; the UI shows
    one number.
14. Rendered (prose) outputs with a raw-JSON toggle.
15. Onboarding-grade empty states (starter workflow, example run).

## What was done in this session (interface quick wins — all 17 applied)

Implemented from the design audit, typechecked (`tsc --noEmit` clean), verified rendering
in the dev server (`/console` basePath):

- **Contrast/AA fixes** on Approvals (zinc-400/500 → `text-muted`).
- **Run IDs are now links** (Approvals → `/runs?run_id=…`, truncated with full-ID tooltip).
- **Deep-linkable runs** — selecting a run writes `?run_id=` via `history.replaceState`.
- **"Resolve in Approvals →"** link on paused runs (was a dead `approval_id` in JSON).
- **Audit rows**: expand chevron, started-at in the header row, human timestamps
  (`fmtTime`, ISO on hover), **Tool calls / Memory interactions / Tokens** now rendered
  (fields the backend recently started populating; the UI previously dropped them).
- **`Skeleton` loading component** used on Runs/Audit/Approvals/Studio (pages previously
  rendered nothing while loading).
- **`Json` capped at `max-h-80`** so large snapshots don't blow up pages.
- **StatusBadge dev-warns on unmapped statuses** (root cause of the policy-kill-looked-
  harmless incident).
- **Money formatting** — `fmtUsd`: cents ≥ $1, sub-cent below (Cost + Audit).
- **Required-field `*` amber → red**; shared `fieldInput` exported (drift removed in
  NodeInspector).
- **Connect button** demotes from primary CTA to "Connection" once configured.
- **Draft banner neutralized** in Studio editor (amber reserved for read-only/published).
- **Node editor dialog**: `aria-modal`, focus moves in on open.
- Off-system `hover:opacity-80` replaced with the standard hover treatment on run rows.

## Recommended sequence

1. **Week 1:** C-1/C-2 auth fixes + budget fail-open logging + studio RBAC (days of work,
   per the tech audit).
2. **Weeks 2–4:** audit-chain lock under fan-out; run-centric console view + designed
   timeline + cost breakdown (the "demoable wow" layer).
3. **Month 2:** OTel export + streaming (interop credibility), graph versioning/diff.
4. **In parallel:** 3–5 design partners in EU-exposed verticals against the Aug 2026
   Annex III deadline; start SOC2 clock.
