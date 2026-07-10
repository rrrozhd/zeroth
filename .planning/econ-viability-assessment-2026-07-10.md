# Zeroth's economic layer: how useful, and what's missing for "economically viable AI apps"

*2026-07-10 — strategic assessment, econ lens only. Grounded in `src/zeroth/core/econ/*`,
`orchestrator/runtime.py`, `service/bootstrap.py`. Companion to the broader sellability
audit in `.planning/product-gaps-2026-07-07.md` (which is ~80% governance/funnel/GTM — this
doc deliberately stays on the money question).*

## Verdict in two sentences

Zeroth has a **best-in-class cost-instrumentation-and-advisory substrate for a governed
runtime** — per-node cost metering, structural waste attribution, and a genuinely novel
right-sizing method that a raw-API or gateway user structurally cannot replicate. But it is
**not yet a viability product**: it is a *cost-reduction* stack (measure, detect waste,
right-size, cap) with a near-absent *value* side, most of its optimization is advisory
rather than automatic, and — verified below — the whole layer is **off by default**.

## The reframe that reorganizes everything

**"Economically viable" ≠ "cheaper." Viability is value ÷ cost.** Almost the entire econ
subsystem lives in the denominator:

| Capability | Where | Denominator (cost) or Numerator (value)? |
|---|---|---|
| Per-node/run/tenant cost metering (litellm pricing) | `econ/cost.py`, `econ/adapter.py` | cost |
| Structural waste detection (failed-run, loop, retry, cache) | `econ/waste.py` | cost |
| Model right-sizing A/B/C | `econ/rightsizing*.py`, `opportunities.py` | cost |
| Budget caps | `econ/budget.py` | cost |
| **Value produced per dollar** | **— nothing —** | **numerator** |

Zeroth measures what a run *costs* to exquisite precision and has no representation of what a
run is *worth*. That single gap is the strongest answer to the literal mission — and it's the
one thing gateway competitors (the crowded proxy-layer category: cost dashboards, per-key
budgets, caching, fallback routing) **structurally cannot do**, because a proxy sees API
traffic, not whether the run succeeded at its business task. Zeroth, as the runtime, holds
`RunStatus`, the eval harness's pass-rate, and Mode B's equivalence scoring — the numerator is
already sitting in the substrate, unjoined.

## The one question that ranks everything: "why in the runtime, not at the gateway?"

Cost tracking, budgets, caching, and fallback routing all exist at the proxy layer today. So
"what's missing" is **not more of that**. Zeroth only wins on the things that require being the
runtime — and those are exactly its two real assets:

1. **Structural waste attribution** — cost-per-*node* in a governed graph; "this run failed but
   still paid"; loop/retry waste. A gateway sees a flat call stream and can't attribute spend to
   graph structure. (`waste.py`)
2. **Audit-trail-as-free-eval-set right-sizing** — "does the cheap model produce output
   *equivalent to the model you already trust*, on your own real traffic?" Zeroth uniquely holds
   pricing + eval harness + per-node audit (real inputs *and* incumbent outputs), so it needs
   zero labeling. A gateway would be guessing. (`rightsizing_experiment.py`, Mode B)

Everything Zeroth should build for this mission either deepens these two, or adds the value axis.
Everything that merely re-implements a gateway feature is a losing use of effort.

## What's missing — four buckets

### 1. Strong cost side, absent value side  *(the headline gap — and cheap)*
No cost-per-**successful**-outcome, no revenue/value attribution, no unit-economics view
("is this node / feature / tenant paying for itself?"). This is a **composition gap, not a
from-scratch build**: the runtime already captures `RunStatus` (success/failure), the eval
harness already scores quality, and `waste.py` already aggregates `cost_usd` per node. Joining
them into cost-per-successful-run and $/outcome is an aggregation over data already captured —
the same shape `waste.py` and `opportunities.py` already are. **This is the highest-leverage,
lowest-cost next build and it's the thing no competitor can copy.**

### 2. It advises, it doesn't act
- **Right-sizing recommends; it never routes.** A human must read the nudge, click "Use," re-test.
  There is no runtime router that sends each request to the cheapest model that will succeed
  (a cascade: cheap-first, escalate on low confidence / failure) — the single biggest live lever
  for cost.
- **Budget caps block; they don't shape.** `BudgetEnforcer` is tenant-level, fail-open by
  design, 30s-cached — a coarse hard stop. No per-run/per-request ceiling, no graceful
  degradation (downgrade model as you approach the cap instead of failing the run), no forecast.
- Caching is exact-match (`CachingProviderAdapter`), not semantic.

### 3. Runtime-deep, but narrow
- **Waste detection is per-run and structural only** — 4 detectors, single-run scope. No
  fleet/cross-run waste ("this node *always* retries"), and — notably — **no token-level waste**:
  prompt bloat, oversized context windows, over-provisioned `max_tokens`, redundant tool calls.
  In practice oversized prompts are often the largest real waste, and `waste.py` doesn't see it.
- **Right-sizing's *measured* mode is tool-free only.** Faithful tool-agent replay is deferred —
  so the "confirmed: switch, here's the $ delta" verdict doesn't yet apply to tool-using agents,
  which are usually the *most* expensive nodes. The flagship differentiator currently reaches the
  cheapest nodes and skips the priciest.
- **No time axis.** Cost page is point-in-time — no spend-over-time, anomaly alerts ("spend
  spiked 4× today"), forecasting, or trend. Economic management needs history.

### 4. Off by default, and unproven
- **Cost attribution is gated on Regulus. Verified today:** `InstrumentedProviderAdapter`
  (the only thing that populates `cost_usd`) is skipped unless `self.regulus_client is not None`
  (`runtime.py:1306-1308`), and `regulus_client` is `None` unless `settings.regulus.enabled`
  (`bootstrap.py:267`, wired at `:305`). So out of the box, `cost_usd` is unpopulated and the
  *entire* econ layer — waste, opportunities, Mode C — reads zeros. Turning the economic story on
  requires `ZEROTH_REGULUS__ENABLED=true` **plus** a running econ_plane backend (with its own sync
  SQLAlchemy / `greenlet` setup gotchas). The most differentiated part of the platform is behind
  a flag most evaluators won't find.
- **Zero demand evidence.** No design partner runs a real workload on this econ layer; the "why
  in the runtime not the gateway" thesis is architecturally sound but unvalidated in market.

## Recommended order (econ lens)

0. **On by default (or trivially so).** A cost layer nobody sees isn't a differentiator. Make
   `cost_usd` populate without a separate Regulus backend for the local/dev path, or make
   enabling it one obvious switch. *Prereq for every claim below being demoable.*
1. **Add the numerator.** Ship cost-per-successful-outcome + per-node/tenant unit economics as an
   aggregation over `RunStatus` × `cost_usd` × eval pass-rate. Cheap, uncopyable, answers the
   mission literally.
2. **Turn the best advice into action.** A runtime model-router / cascade — the single biggest
   automatic cost lever, and the natural payoff of Mode B once it graduates to confirmed.
3. **Close the tool-agent replay gap** so the measured recommendation covers the expensive nodes.
4. **One proof point.** A real workload where "we cut this app's $/successful-run by X% and proved
   equivalence on your own traffic" is a sentence a buyer believes. That validates the whole thesis
   more than any additional feature.

**Bottom line:** the econ subsystem is real, differentiated, and unusually honest (the
confirmed-vs-flagged discipline in `waste.py` and the incumbent-as-reference method are genuinely
good engineering). To *fulfill* the mission it needs to (a) come on by default, (b) grow a value
axis so it can speak to viability and not just cost, (c) act on its own recommendations instead of
only surfacing them, and (d) meet one real workload that proves the runtime wedge beats the gateway.

---

## Update (2026-07-10) — #1 (value axis / unit economics) shipped

Recommendation #1 is implemented end-to-end (v0.7.5). Cost-per-**successful**-outcome now
exists, built as an aggregation over data already captured — exactly the "cheap composition"
the assessment argued for.

- **Model** — `src/zeroth/core/econ/unit_economics.py`: pure `unit_economics(runs, audits)`.
  An outcome is a **top-level run** (`parent_run_id is None`); authoritative success/failure
  from `RunStatus`; per-run cost = Σ its audit `cost_usd` (same attribution as `waste.py`).
  Headline `cost_per_successful_run_usd = terminal spend ÷ successes` (loads the failure tax
  onto each good result); in-flight spend is a separate line, never in the metric; `None`
  when nothing has succeeded. `failure_tax_usd` = fleet-level `paid_for_failed_run`.
- **API** — `GET /v1/econ/unit-economics` (`METRICS_READ`, read-only) in
  `src/zeroth/core/service/econ_analytics_api.py`; reads `run_repository` + `audit_repository`
  (not the Regulus proxy). Registered on v1 + compat routers.
- **Console** — "Unit economics" card on the Cost page (`frontend/app/cost/page.tsx`);
  `getUnitEconomics()` in `app/lib/api.ts`.
- **Honesty rail = the #4 gap, surfaced in-product**: when runs exist but no cost is
  attributed, the card says *"cost tracking looks disabled — enable ZEROTH_REGULUS__ENABLED"*.
- **Tests** — `tests/test_econ_unit_economics.py` (8), `tests/test_econ_analytics_api.py` (5).
- **Verified E2E** (seeded backend, real HTTP + console render): 2 successes + 1 paid failure →
  **$0.0220 fully-loaded cost/success vs $0.0120 clean** (the $0.010 gap = failure tax),
  40% success rate, 45% of spend on failures. Empty/failed/no-cost states render their honest
  notes. Screenshot proof captured.

Still open from #1's spirit: joining **eval pass-rate** (quality), not just run success, as a
richer numerator; per-tenant unit economics ("which customer is unprofitable"). Recommendations
#0 (on by default), #2 (runtime router), #3 (tool-agent replay), #4 (proof point) remain.

---

## Update 2 (2026-07-10) — gaps #0, waste surface, per-tenant, quality all closed (v0.8)

Four more gaps closed end-to-end (verified over real HTTP + console render, no console errors):

- **#0 — cost tracking on by default.** `CostEstimator` (local litellm pricing, no backend) is
  now always constructed (`bootstrap.py`), the provider-wrap gate dropped its `regulus_client`
  requirement (`runtime.py`), and `InstrumentedProviderAdapter` tolerates a `None` client
  (stamps cost, emits no event). So `cost_usd` — and every local lens — populates out of the box;
  Regulus stays optional (event stream + budget caps only). The `runs_with_cost==0` notes were
  reworded to stop blaming Regulus (it's no longer the gate).
- **Waste surface.** `waste_rollup()` + `GET /v1/econ/waste` + an "Economic waste" Cost-page card
  — the one analysis lens that was Python-only now has an API and UI (confirmed vs flagged totals,
  per-kind, top findings).
- **Per-tenant unit economics.** `by_tenant` breakdown on `UnitEconomicsReport` + card table —
  "which customer is unprofitable" (verified: globex $0.06/success + tax vs acme $0.013, no tax).
- **Quality-aware outcomes.** `POST /v1/econ/quality-verdict` attaches an external good/bad verdict
  to a run's metadata (off the audit chain); `unit_economics` overlays cost-per-*good*-outcome over
  the **labeled** subset only, with coverage disclosure and a floor — honest by construction (no
  verdict → excluded from both numerator and denominator; never a faked score). Automated scoring
  (an inline scorer node writing `validation_results` pre-seal) is documented as the v2 path.

Also fixed in passing: a **pre-existing latent circular import** (`config.settings` ↔
`pgvector_connector`, and the identical twin in `chroma_connector`) that the earlier
`econ_analytics_api` import surfaced — both connectors now import the embedding defaults lazily,
breaking the cycle permanently. Full suite: 1633 pass.

**Verification honesty:** #0's *attribution* path (a live graph run, Regulus off, the adapter
stamping `cost_usd` through the relaxed gate) is **unit-verified**, not E2E — three unit tests
cover the pieces (adapter stamps with a `None` client + emits no event; bootstrap wires
`cost_estimator` while leaving `regulus_client`/budget off; the gate now needs only
`cost_estimator`). The browser E2E used **seeded** `cost_usd` on the audit records, so it
verified the read/aggregation/render path (all four cards), not live attribution. A full
orchestrator-run acceptance test (deterministic provider, assert persisted `cost_usd` non-None)
is the remaining gap.

**Still open:** #2 runtime model-router (act, don't just advise), #3 faithful tool-agent replay,
#4 one real proof workload.
