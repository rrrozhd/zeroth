# Zeroth — What's Missing to Be a Distinctive, Sellable Product (2026-07-07)

> **Status update (same day, v0.5.1–v0.5.3.1):** most engineering closures below
> are now implemented — see commits `23ca181..14ad969`. Closed: A.1 publish
> endpoint, A.2 entry_step authoring, A.3 contract listing, A.5 runner factory
> (model_provider is real), A.6 deploy/rollback API; B budget caps trip
> end-to-end, tenant-true audits, chain-verify endpoints, hardened docker
> sandbox + truthful README; C real CLI (serve/seed-demo/migrate), SQLite
> boot migrations, Dockerfile+compose (container-verified), docs env-var/
> healthz/provider fixes, readiness no longer fails on disabled redis; plus a
> latent entrypoint bug (uvicorn --factory + asyncio.run) found and fixed.
> Still open: A.4 JSON-Schema contracts, A.7 serving hot-swap (deploy still
> binds one deployment per process at restart), console UI wiring for
> publish/deploy/diff/verify, D (triggers, approval notifications, retention,
> per-tenant LLM keys, SDKs, streaming), E surfacing (evals UI), and all of F.

Synthesis of a 5-agent codebase assessment (gap re-check, last-mile authoring, moat depth,
first-hour DX, sellable-surface scan) run on branch `feat/console-frontend`, building on the
2026-07-06 product audit (`.planning/product-audit-2026-07-06/`). All claims cite current code.

## Verdict in one paragraph

The runtime is further along than the market audit assumed — all five v0.4 credibility blockers
are verified fixed, and evals, OTel tracing, graph diff, durable crash-resumable workers, and
cost metering all exist in code. What's missing is not features; it's **closure**: (1) the
product loop doesn't close — a canvas draft cannot become a running API without five Python
steps and a process restart, so today Zeroth sells as a framework with a governed viewer, not a
product; (2) the moat claims don't close — budget caps are effectively inert with the bundled
econ plane, audit records stamp `tenant_id="default"`, and the default "sandbox" is a plain
subprocess, so the three pillars of the pitch would fail a skeptical CISO's first hour of due
diligence; (3) the funnel doesn't close — the pip path, documented CLI, fresh-install boot, and
documented auth env var are all broken, so evaluators bounce before seeing any of the good
parts. Distinctiveness exists in the code (hash-chained audits, deployment attestation, evidence
bundles, first-class approval nodes) but is half of it is unexposed — no API, no UI, no docs.

## A. The product loop doesn't close (canvas → running API) — the defining gap

Today's honest sequence after saving a canvas draft: register contracts (Python) → publish
(Python) → create deployment (Python) → write a custom entrypoint registering `agent_runners`
per node_id (Python) → restart the process with `ZEROTH_DEPLOYMENT_REF`. Evidence:

1. **No publish endpoint.** `GraphRepository.publish` exists (`graph/repository.py:87-101`) but
   `studio_api.py` exposes only list/create/get/update/clone/delete/node-types. *Effort: S.*
2. **Canvas drafts can never validate.** `entry_step` is required by `GraphValidator`
   (`validation.py:448-456`) but unsettable from studio API or console. *S.*
3. **No contract-ref authoring.** Validator requires per-node `input/output_contract_ref`
   (`validation.py:304-321`); API round-trips them; console has no fields and there's no
   endpoint listing `ContractRegistry` names. *S.*
4. **Contracts are Pydantic-class-only.** `ContractRegistry.register` takes a class path
   (`contracts/registry.py:152-175`); no write API; a JSON-Schema contract from the canvas has
   no path in. *M (pydantic.create_model synthesis or schema-validation fallback).*
5. **Agent runners are code-registered per node_id and the stock entrypoint registers none**
   (`runtime.py:1164-1167`, `entrypoint.py:59-61`). Any canvas graph with an agent node cannot
   execute on the stock service. **The canvas `model_provider` field is decorative — the runtime
   never reads it** (0 grep hits in runtime/agent_runtime). *M: bootstrap-time runner factory
   building AgentRunner + LiteLLM adapter from `AgentNodeData`.*
6. **No deploy API, no hot-swap.** `DeploymentService.deploy` is code-only; serving is
   one-deployment-per-process bound at bootstrap; "deploy" = edit env var + restart. *S/M for
   POST /deployments; M/L for serving swap.*

Ship 1→5 and the demo becomes "draw it, publish it, run it" — which no governed competitor has.
Until then the console authoring investment (v0.4.2–v0.4.10) is a beautiful road to a cliff.

## B. Moat claims that would fail due diligence (make the pitch true)

- **Budget caps are effectively inert end-to-end.** Enforcement points exist (pre-LLM-call
  `runner.py:216-228`, pre-fan-out `runtime.py:678-686`), but the bundled econ_plane never
  stores or returns `budget_cap_usd`/`total_cost_usd` and ignores `tenant_id` — so with the
  in-repo backend the check can never trip. Plus fail-open by design and a 30s cache. The #1
  headline claim ("runtime-enforced budget caps") is currently demo-proof only against a
  hypothetical external Regulus. *Critical.*
- **Audit tenant attribution is broken.** Orchestrator-written node audits omit `tenant_id`
  → stamped `"default"` (`runtime.py:792-806,1720,1787,1874`). Evidence bundles inherit this.
  For the EU-AI-Act buyer, "whose decision was this" is the whole point. *Critical.*
- **Multi-tenancy is API-layer only.** Memory connectors have zero tenant namespacing
  (`memory/redis_kv.py:49-50`; no `tenant` hits in `memory/*.py`) — SHARED-scope memory is
  cross-tenant readable. Graphs have no tenant column. *High.*
- **Sandbox default contradicts the README.** Default backend is LOCAL subprocess
  (`sandbox.py:148`); native Python units run in-process; Docker path lacks `--read-only`,
  `--cap-drop`, seccomp, non-root. README says "every executable unit runs inside a governed
  sandbox." Either harden defaults or soften the claim. *High.*
- **Capabilities are self-declared** by the node's own `capability_bindings` (`guard.py:62-64`)
  — a node that declares nothing needs no permission; nothing intercepts actual behavior
  outside the sandbox path. *High.*
- Chain verification is test-only (no API endpoint); attestation digests are unsigned
  (recomputable hashes, `provenance.py:61-63`). *Medium — cheap to expose, big credibility.*

## C. The funnel is broken at the front door (cheapest fixes, highest impact)

- **pip path is broken**: wheel ships no `examples/`; docs' hello snippet →
  `ModuleNotFoundError`. Bounce at ~minute 10.
- **Documented CLI doesn't exist**: `zeroth-core serve` referenced in 6 doc pages; pyproject has
  no `[project.scripts]`. Bounce #2. No `zeroth init` scaffold for the 40-line bootstrap dance.
- **Fresh-install boot crashes twice**: SQLite path never creates schema; deployment "default"
  never seeded → `DeploymentBootstrapError`. Docs claim schema auto-creation and a `/healthz`
  route that doesn't exist (it's `/health`).
- **Documented auth env var is wrong twice**: docs say `ZEROTH_AUTH__API_KEYS_JSON` (dict);
  code reads `ZEROTH_SERVICE_API_KEYS_JSON` (list of credential objects) — following the guide
  yields a service that 401s everything.
- **No Dockerfile/compose/helm anywhere**, while three docs/docstrings reference a phantom
  Docker image.
- **PyPI publish still blocked** on trusted-publisher registration (both zeroth-core and
  zeroth-console) — the distribution channel itself isn't live.
- Counterpoint: the repo-checkout `quickstart.sh` path is genuinely one command and strong;
  the 20-example suite is real. But examples are domain-toys (echo/Q&A) — no business-shaped
  reference app (ticket triage, document pipeline) to make the value legible.

## D. Product edges that block real deployments

- **No triggers.** The only way to start a run is client POST /runs — no cron/schedules, no
  inbound webhooks, no queue consumers. "Run this every morning" requires the buyer to build a
  scheduler. *High for sellability; a schedule + inbound-webhook trigger is a natural node type.*
- **Approval gates can't reach humans.** Notification = outbound HMAC webhooks only; no
  email/Slack channel. The flagship governance feature has a built-in "run silently stuck on
  approval" failure mode unless every customer builds a bridge. *High.*
- **No streaming anywhere** (grep SSE/WebSocket: zero hits; console polls at 1.5s). Reads as
  prototype in any bake-off. *Medium-high.*
- **No retention/purge for audits/runs/cost events** — unbounded growth is an ops problem and a
  GDPR/data-minimization contradiction for the exact compliance buyer being targeted. *High.*
- **LLM keys are process-global env vars** — no per-tenant/per-deployment keys, no vault/KMS
  provider. *Medium.*
- **No published client SDKs** (raw HTTP + OpenAPI dump; console generates internal TS types).
  *Medium.*
- SSO: OIDC/JWT verification exists (incl. JWKS); SAML absent; roles are a hardcoded 3-member
  enum with no custom roles or management API. *Medium; enterprise-gate.*

## E. Built but invisible — expose what already exists

| Capability | Exists as | Missing surface |
|---|---|---|
| Eval harness (`zeroth.core.eval`, LLM-judge, CI gate) | Python lib | HTTP API, console page, docs, baseline-diff regression |
| Graph diff (`graph/diff.py`, typed DiffEntry) | Python lib | Endpoint + version-history/diff/rollback UI |
| Audit chain verifier | Tests only | `POST /runs/{id}/verify-chain` + console "verified ✓" badge |
| Version rollback (`clone_published_to_draft`) | API | Console version-history view |
| OTel tracing (OTLP, spans across run→node→tool) | Off by default | OpenInference/GenAI semantic conventions; a "works with Datadog/Arize" docs page |
| Cost metering per node/run/tenant | Emitted to econ plane | Console per-node/per-run breakdown + budget bar (needs B-1 fixed first) |
| Deployment attestation + evidence bundles | API | The **compliance pack**: exportable Art. 12 evidence bundle, signed attestation — the sales artifact for the Aug 2, 2026 wedge |

## F. Commercial/trust layer (not code, still missing)

Design partners (0 today) against the Aug 2026 EU AI Act deadline; the Art. 12/14 mapping
whitepaper; SOC2 clock; pricing/metering enforcement (metering hooks exist, no EE gating);
a written API deprecation policy; case studies. Apache-2.0 + clean history is done.

## G. The skeptic's cut (adversarial review of this analysis)

A deliberately hostile review (buyer + investor lens) of the sections above surfaced blind
spots the code-reading methodology can't see. Recorded here at full strength:

1. **Zero demand evidence for the wedge — and Aug 2 is 26 days away.** No design partner, no
   prospect has ever asked for runtime-level Art. 12 evidence bundles. In practice Art. 12/14
   may be satisfied by organizational process (QMS, tech documentation) that never touches the
   runtime. Enterprise procurement alone takes longer than the remaining window; either
   design-partner recruitment becomes the primary job *now*, or the honest call is that the
   Aug 2026 cycle is missed and the compliance pack re-aims at the next one.
2. **No GTM motion chosen → contradictory roadmaps.** Self-serve developer buyer ⇒ C and A
   matter, B's tenant depth mostly doesn't (solo devs have one tenant). Sales-led regulated
   enterprise ⇒ nobody pip-installs at minute 10; what matters is a reference architecture, a
   filled security questionnaire, and services support. Which half of this report is noise
   depends on a decision it can't make.
3. **"Medium-code" has no identified persona.** The canvas buyer will compare against n8n/Retool
   and find no triggers/integrations/template library (Section D) and churn even after A ships;
   the Python buyer doesn't want a canvas, they want the broken SDK ergonomics of C fixed.
   Completing the canvas loop without a single user requesting it risks being sunk-cost
   reasoning dressed as strategy.
4. **Vendor viability IS the diligence.** A single-maintainer Apache-2.0 project asking
   enterprises to hang their regulatory evidence chain on it fails the same procurement review
   that Section B is meant to pass. And "governance inside the runtime is unowned" has no
   half-life attached — LangGraph Platform or any gateway can bolt on a governance checkbox
   faster than a solo founder builds distribution. A–F is 3–4 FTE-quarters plus a sales motion;
   a cut list is mandatory.
5. **No SKU, no severity ranking.** Whether SSO/SAML is "medium" or existential, whether the
   funnel matters at all, depends on whether the console is the paid product or a free viewer
   and whether ACV is $99/mo or €50k. Every priority above silently assumes an answer.

Hardest buyer question the analysis doesn't answer: *"Who else runs their production audit
trail on this — and if the one maintainer disappears, what happens to my compliance evidence,
versus waiting for my existing vendor to ship the same checkbox?"*

## Recommended order (revised after the adversarial pass)

0. **Truth-in-labeling — today, hours of work.** Fix the README sandbox claim, caveat the
   budget-cap story until B-1 lands, remove the six references to the nonexistent
   `zeroth-core serve`. A governance vendor caught overstating is unfundable; this can't wait
   behind engineering.
1. **One production proof point over any feature.** A design partner (or the founder's own
   real workload) running one business-shaped graph in production — Python-only is fine, since
   engineers are the current persona. This is the only thing that de-risks the demand thesis,
   produces the case study, and answers the hardest buyer question. Everything else supports it.
2. **Make the pitch true (B)** — budget caps that actually trip against the bundled econ plane;
   tenant-true audits; expose chain verification. Required before any external diligence.
3. **Unbreak the funnel (C)** — real CLI (`zeroth serve`/`zeroth init`), fix wheel/docs/env
   drift, ship a Dockerfile+compose, register PyPI trusted publishers. Days of work; enables
   bottoms-up discovery regardless of GTM choice.
4. **Close the loop (A.1–A.5)** — publish endpoint, entry_step + contract pickers, JSON-Schema
   contracts, runner factory. The category-defining demo (draw → publish → run, governed) —
   but sequenced after proof, not before, and only if the canvas persona survives the GTM
   decision in G-2/G-3.
5. **Kill the dead-end runs (D)** — approval email/Slack notifier, schedule trigger, retention
   policy — prioritized by what the proof-point workload actually hits.
6. **Surface the buried moat (E)** — evals UI, diff/rollback UI, compliance pack export — as
   sales artifacts once there's someone to sell to.

The distinctive core — governance enforced *inside* the runtime that also deploys the service —
is architecturally real and (as of today) unowned in the market. What it's missing, in order:
claims that survive contact with a buyer's security team, one customer who proves the demand
thesis, a funnel that doesn't break at minute 10, and only then the last mile that lets that
customer experience it without writing Python.
