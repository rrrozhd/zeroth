# How you actually analyse the economics of an app on Zeroth

*2026-07-10 — a practical method, grounded in a code-verified map of all six econ surfaces
(map + adversarial verify workflow, 12 agents; 5/6 surfaces CONFIRMED, budget PARTIALLY_WRONG
on one non-material bootstrap line). Every endpoint, page, and caveat below was checked against
current code on `feat/console-frontend`.*

## The one-sentence version

You **turn cost tracking on**, let the runtime **auto-meter every LLM call** onto the audit
trail, then read four **observation lenses** (aggregate spend → unit economics → waste →
right-sizing opportunities), act on them with the **right-sizing experiment**, and **enforce** a
budget cap going forward — a loop, not a dashboard.

## Stage 0 — Turn it on (the precondition that decides everything)

**Nothing below works until Regulus cost tracking is enabled — and it is OFF by default.**
Cost attribution is gated twice: the bootstrap only builds the `RegulusClient`+`CostEstimator`
under `if settings.regulus.enabled` (`bootstrap.py:267`, default `False` at
`econ/models.py:19`), and the runtime only wraps a node's provider when both are non-None
(`runtime.py:1305-1309`). With it off, `NodeAuditRecord.cost_usd` stays `None` everywhere and
**every lens shows zero**.

To enable (in-process/bundled mode):
```
ZEROTH_REGULUS__ENABLED=true
ZEROTH_REGULUS__BASE_URL=http://<host>/regulus/v1     # must include /regulus for the mount
ECP_DATABASE_URL=sqlite:///…    ECP_JWT_SECRET=…      # bundled econ_plane (sync engine + greenlet)
```

## Stage 1 — Measure (automatic; the raw material)

Not user-invoked. When a graph runs with Regulus on, `InstrumentedProviderAdapter` wraps each
agent node's provider and, per LLM call: computes USD via `litellm.cost_per_token`
(`cost.py`), measures wall-clock latency, fires a fire-and-forget `ExecutionEvent` to Regulus,
and stamps `cost_usd` + `cost_event_id` + `token_usage` onto that node's `NodeAuditRecord`.
**This is the only place a real per-call dollar figure enters the system**; everything else reads
these records. (Cost is a litellm *estimate*, not the provider's invoice; cache hits attribute $0
and record `cache_saved_usd` instead.)

## Stage 2 — Observe & attribute (read-only lenses)

| Lens | Question | How you run it | What it tells you |
|---|---|---|---|
| **Aggregate spend** | "What did this deployment cost, cumulatively?" | Console **Cost page** top card / `GET /v1/deployments/{ref}/cost` (queries Regulus KPIs; `METRICS_READ`, admin-tier) | One running total. The high-level bill. |
| **Unit economics** | "What does one *good outcome* cost — including the failures I paid for?" | Console **Cost page** "Unit economics" card / `GET /v1/econ/unit-economics` (local run+audit join) | `cost_per_successful_run` (terminal spend ÷ successes), success rate, **failure tax** ($ + % of spend that bought nothing), per-workflow breakdown. |
| **Structural waste** | "How much of a run's spend was wasted, and how much can I say so without a caveat?" | ⚠️ **Python only — no API, no console**: `from zeroth.core.econ import analyze_run, waste_gate` | Per-run `confirmed_waste` (paid-for-failed) vs `flagged_waste` (loops/retries); `waste_gate` can fail a CI pipeline on a $ or ratio threshold. |
| **Right-sizing opportunities (Mode C)** | "Which nodes cost the most *and* have a cheaper capable model — where do I even look?" | Console **Cost page** "Right-sizing opportunities" card / `GET /v1/econ/rightsizing/opportunities` (no LLM) | Nodes ranked by spend, each with `best_savings_pct`, an **upper-bound** projected saving, and an `experiment_ready` flag. This is the triage list. |

## Stage 3 — Optimize (act on a target)

| Mode | Question | How you run it | Cost |
|---|---|---|---|
| **A — design-time nudge** | "For this model, what cheaper models keep the same capabilities?" | Studio **node inspector** nudge under the model field / `POST /v1/econ/rightsizing` | Free (litellm lookup; capability gate *before* price) |
| **B — measured experiment** | "Does a cheaper model produce answers *equivalent to the one I trust*, on MY real traffic?" | Node inspector **"Measure equivalence"** / `POST /v1/econ/rightsizing/experiment` | Real LLM calls: replays harvested audit inputs through candidates + an equivalence judge, scored against the incumbent's own *self-equivalence ceiling*. Verdict `confirmed`/`flagged`/`none`. |

The three modes are a funnel of rising cost & confidence: **C** (where to look) → **A** (cheaper &
capable?) → **B** (actually as good?). Mode B's big limit: **tool-free replay only** — it skips
any run where the incumbent called tools, so the priciest tool-using agents aren't yet testable.

## Stage 4 — Enforce (shape spend going forward)

Set a cap: `PUT /v1/tenants/{tenant_id}/budget` (`{"budget_cap_usd": N}`, `METRICS_ADMIN`).
Then `BudgetEnforcer.check_budget` automatically gates the two places money is about to be
spent — **before each agent LLM call** (`runner.py:217`) and **before spawning fan-out
branches** (`runtime.py:715`) — and hard-fails the run on breach. Caveats: **fail-open** (a
Regulus outage → no enforcement, logged as a warning), **30s TTL cache** (a burst within the
window slips past), tenant-level only, and inert unless Regulus is on *and* a cap is set.

## The loop, and its honest edges

**enable → measure → observe (spend / unit-econ / waste / opportunities) → optimize (right-size)
→ enforce (cap) → re-measure.**

What this method can and cannot tell you — state these plainly to anyone relying on it:
- **It's off by default.** The single biggest gotcha: no Regulus → every number is zero.
- **Cost, not value.** An "outcome" is `RunStatus` — a *wrong-but-completed* answer counts as a
  success. The whole stack measures spend per success, never the business value of a success.
- **Estimated, not billed.** Dollars come from litellm's static price table, not the provider invoice.
- **Coverage gaps.** Waste has no UI/API (Python only); right-sizing B is tool-free only;
  unit economics is windowed (last ~200 runs) and excludes sub-graph child spend; budget is
  coarse + fail-open.

In short: the economics are analysed by **joining the runtime's own audit trail against a pricing
table**, at four granularities (call → node → run → deployment), then closing the loop with a
measured model swap and a hard cap. The differentiated part — what a gateway can't do — is the
structural attribution (cost per *node* / per *successful outcome* / per *failed run*) and the
audit-trail-as-eval-set right-sizing. The missing part is the value side and turning it all on
by default.
