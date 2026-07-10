# Model right-sizing — estimating & guiding cost cuts

*2026-07-09 — design note. Operationalizes the phase `waste.py` explicitly defers:
"Model right-sizing (cost × eval pass-rate) … deferred to its own phase" (src/zeroth/core/econ/waste.py:19-20).*

## The problem, stated honestly

"Suggest a cheaper model that does the same thing just as good" is two claims with
wildly different costs:

- **"cheaper"** is *free*. `litellm.cost_per_token` prices any of ~2,949 models. We
  already do this (`econ/cost.py`). No design needed.
- **"just as good"** is the *entire* problem. It's undefinable without a definition of
  *good for this task* — and for a "basic mundane task on gpt-5.5" the user has **no
  labeled eval set and never will write one.** A design that requires them to author
  `EvalCase`s with `expected` outputs is dead on arrival.

**The unlock — incumbent-as-reference.** Don't ask "is the cheap model good?" (needs
ground truth). Ask "does the cheap model produce output *equivalent to the model the
user already trusts*, on their own real traffic?" That needs **zero labeling**: the
audit trail already stores real inputs *and* the expensive model's real outputs. Harvest
N real inputs, replay them through the candidate, score equivalence. The user's bar is
literally "as good as what I run now" — so equivalence-to-incumbent *is* the correct
definition of good, not a compromise.

Zeroth can do this because it's the *runtime*: it uniquely holds the pricing substrate
**and** the eval harness **and** the per-node audit trail. A raw API user can't — they'd
be guessing.

## Three modes, ranked by confidence (reusing `waste.py`'s confirmed/flagged vocabulary)

The same honesty rule `waste.py` applies to waste — never let a heuristic dress up as a
measurement — carries straight over to recommendations.

### Mode A — Design-time nudge  → *flagged: worth testing*

When the user drops gpt-5.5 onto a node, infer **task shape** from the node config
(prompt length, tool bindings, structured-output schema, temperature, needs-vision) and
cross-reference litellm's capability+price table to surface cheaper *capability-compatible*
candidates.

> *"This node looks like a simple extraction (no tools, structured output, temp 0).
> `gpt-5-mini` and `claude-haiku` are capability-compatible and ~18× cheaper. Want to
> A/B it against your real traffic?"*

- **Never claims equivalence** — it's a prompt to test, nothing more.
- **The table is *derived from litellm*, not hand-maintained.** `get_model_info(model)`
  returns `input_cost_per_token`, `output_cost_per_token`, `supports_function_calling`,
  `supports_response_schema`, `supports_vision`, `max_input_tokens`, `mode`, … for all
  ~2,949 models. Verified 2026-07-09. So the catalog can't rot the way a hand-written one
  would.
- Cost to build: low. Cost to run: zero. Epistemic weight: low — hence *flagged*.

### Mode C — Passive targeting from the audit trail  → *where to point Mode B*

Not a separate feature — it's how Mode B gets its dataset. Aggregate real runs by node
from `NodeAuditRecord.cost_usd` (already captured; `waste.py` already reads it): `$/run`,
`$/month at current volume`, rank nodes by spend. The same audit records also carry the
real **inputs** and the incumbent's real **outputs** → that's the free eval dataset that
feeds the replay. So "expensive node X" comes bundled with everything needed to test a
swap on X.

### Mode B — The measured recommendation  → *confirmed: switch, here's the $ delta*

Replay the N harvested real inputs through each candidate (same prompt, same tools),
score **equivalence-to-incumbent** with an LLM-judge scorer, on the *existing* harness
(`eval/runner.py` `run_eval` → `EvalReport` → `gate`). The recommendation rule:

1. **Capability gate first.** Disqualify any candidate that can't call the node's tools,
   can't emit its structured output, or can't fit its context (from litellm flags). *Price
   is irrelevant if it can't do the job.*
2. **Quality band.** Among capability-compatible candidates, keep those whose
   `equiv_rate` ≥ `incumbent − tolerance`, measured over **N ≥ K** real cases.
3. **Rank by cost, recommend the cheapest** in the band.
4. **Report the row:** `(model, equiv_rate, $/1k runs, p50 latency, capability-ok)`.
   Latency is already measured by `InstrumentedProviderAdapter` — free axis.
5. **Honesty guard:** below K cases, the result stays *flagged: worth testing* — it never
   graduates to *confirmed: switch*. (Extends the harness's existing
   errored-counts-as-not-passed discipline: a thin sample can't manufacture a
   recommendation.)

**The formula, plainly:**

> Recommend candidate **C** over incumbent **I** for node **N** iff
> **C is capability-compatible with N**, **and** `equiv_rate(C→I over N≥K real cases) ≥ 1 − tolerance`,
> **and** `cost(C) < cost(I)`.
> Estimated saving = `(cost(I) − cost(C)) × monthly_volume(N)`.

Output the user actually sees:

> *"Node `extract_vendor_terms` — 12,400 runs/mo on gpt-5.5 = **$186/mo**.
> Tested `gpt-5-mini` on 200 of your real inputs: **96% equivalent** (vs a 97% self-consistency
> floor), p50 latency 0.8s vs 1.4s, 18× cheaper. **Switch → save ~$176/mo.**"*

## Two definitions of "good" — pick per user

| Scorer | Needs labels? | When |
|---|---|---|
| **Equivalence-to-incumbent** (default) | No | The "mundane task" case. Judge: "are these two outputs equivalent for this task?" Inherits the incumbent's errors — which is fine, the bar is "as good as today." |
| **Absolute-quality** | Yes (expected outputs) | Only when the user *brings* a labeled dataset. Higher bar, catches cases where the incumbent itself is wrong. |

## What already exists vs. what's missing

**Exists (the substrate is all here):**
- `econ/cost.py` `CostEstimator` — prices *any* model, incl. hypothetical alternatives.
- `econ/waste.py` `EconReport` — the confirmed/flagged honesty pattern + per-node cost.
- `eval/runner.py` `run_eval` / `gate`, `eval/models.py` `EvalReport.pass_rate` — the
  scoring harness; `target` is any callable, so "wrap agent, swap model" is trivial.
- `econ/adapter.py` `InstrumentedProviderAdapter` — per-call cost **and** latency.
- Audit trail — real inputs + incumbent outputs + `cost_usd` per node. The free dataset.

**Missing (the deferred phase):**
1. **Replay harness** — harvest N audit inputs for a node, run candidates through the same
   prompt/tools. New `econ/rightsizing.py`, sibling to `waste.py`.
2. **Equivalence-judge scorer** — added to `eval/scorers.py`.
3. **Capability+price catalog view** over litellm `get_model_info` (derived, cached).
4. **Recommendation ranker** — capability gate → quality band → cost sort → $ delta.
5. **Console surfaces** — Mode A nudge in the node inspector; a per-node "right-sizing"
   report card (Mode B/C) in the econ view.

## Implementation status

**All three modes (A, B, C) are built and verified end-to-end, including a live run against
a real model (gpt-4o-mini).** Faithful tool-agent replay is the remaining slice.

### Real-model verification (2026-07-10)

Ran the demo agent (`openai/gpt-4o-mini`) 11× to build audit history, with the bundled
Regulus enabled for cost attribution, then exercised the full flow in the console:

- **Mode C (Cost page)** — the `agent` node surfaced: 11 runs, $0.0003 spend, "up to −100%",
  `testable`. (Reads spend straight from the audit trail, so it works even when the Regulus
  cost-total card doesn't.)
- **Mode B (node inspector)** — a real experiment (real replays + real LLM judge over 5
  harvested runs) returned **CONFIRMED**: `gpt-5-nano` matched `gpt-4o-mini` at 100% (vs a
  measured 100% self-consistency ceiling), ~62% cheaper → recommended `gpt-5-nano`;
  `gpt-4.1-nano` also 100% at ~33% cheaper.

Ops notes for reproducing the real-model run: cost attribution (`cost_usd` on
`NodeAuditRecord`) is gated on Regulus being enabled (`runtime.py:1307`), so Mode C needs
`ZEROTH_REGULUS__ENABLED=true`. The bundled Regulus/econ_plane uses a **synchronous**
SQLAlchemy engine — set `ECP_DATABASE_URL=sqlite:///…` (NOT `sqlite+aiosqlite://…`, which
throws `MissingGreenlet` at startup), and `greenlet` must be installed in the venv.

### Mode C — passive spend opportunities (shipped)

- `src/zeroth/core/econ/opportunities.py` — `spend_opportunities()`: attribute audit spend
  per node, find dominant model, memoize `recommend()` per (model, tools), rank by spend.
  `NodeSpend.experiment_ready` = tool-free runs exist AND a cheaper capable model exists.
  `projected_savings_usd` is an explicit upper bound (all-switch, equivalence unverified).
- `GET /v1/econ/rightsizing/opportunities` (`METRICS_READ`, read-only, no LLM).
- Console: "Right-sizing opportunities" card on the Cost page ([cost/page.tsx](frontend/app/cost/page.tsx)).
- Tests: `tests/test_econ_opportunities.py` (6) + opportunities cases in `test_rightsizing_api.py` (3).

### Modes A and B

### Mode B — measured experiment (shipped)

- `src/zeroth/core/econ/rightsizing_experiment.py` — harvest audit records → `EvalDataset`
  (input + incumbent output), `EquivalenceScorer` (symmetric LLM judge, normalizes both
  sides to answer-text), `run_experiment()` reusing `eval/runner.py`. Honesty rails:
  **tool-free harvest only**, **incumbent replayed as its own baseline** (self-equivalence
  ceiling; tolerance is measured against *that*, not 100%), **verdict is "flagged" below
  `min_cases`** (never "confirmed: switch"), **cost projected on the incumbent's real
  harvested token profile**. All-errored replay → explicit "provider unreachable / missing
  key" note, not a false "nothing cheaper".
- `POST /v1/econ/rightsizing/experiment` in `service/rightsizing_api.py` — flat, derives the
  deployment from bootstrap, harvests via `audit_repository`, builds `LiteLLMProviderAdapter`
  for replay + judge. Every branch is a 200 with a `note` (unpriced / no candidates / no
  history / no key), never a 500.
- Console: `ExperimentPanel` in `ModelRightsizing.tsx` ("Measure equivalence on real
  traffic") with a verdict badge (confirmed/flagged/no-match) + per-model equivalence/cost
  table; `runRightsizingExperiment()` in `api.ts` with a 180s timeout (real LLM calls).
  `nodeId` threaded through `NodeInspector` from the studio editor.
- Tests: `tests/test_econ_rightsizing_experiment.py` (12, fully offline via
  `CallableProviderAdapter`), `tests/test_rightsizing_api.py` experiment cases (4).
- Verified live: endpoint returns "needs real traffic" with no history and "no pricing" for
  an unknown incumbent; console renders the verdict panel over a clean cross-origin round
  trip. Populated ranking (confirmed/flagged/tolerance-vs-ceiling) proven by the offline
  suite — a live populated run needs an LLM key + real run history.

### Mode A — design-time nudge (shipped)

- `src/zeroth/core/econ/rightsizing.py` — `recommend()` + `RightsizingResult`/`ModelOption`,
  derived from `litellm.model_cost` (capability gate → savings floor → same-provider-first
  ranking; collapses dated snapshots and drops `ft:` rows).
- `src/zeroth/core/service/rightsizing_api.py` — `POST /v1/econ/rightsizing`
  (`WORKFLOW_READ`), registered in `app.py` on the v1 + compat routers.
- `frontend/app/components/ModelRightsizing.tsx` — the nudge, wired under the agent's
  model field in `NodeInspector.tsx`; `getRightsizing()` in `app/lib/api.ts`.
- Tests: `tests/test_econ_rightsizing.py`, `tests/test_rightsizing_api.py` (16 + 4).
- Verified live: for tool-using `openai/gpt-4o` ($4.38/M tok blended) the console offered
  `gpt-5-nano` (−97%), `gpt-4.1-nano` (−96%), `gpt-4o-mini` (−94%); "Use" rewrote the
  field and persisted the draft.

## Suggested build order

1. Capability+price catalog over litellm (unblocks A immediately, pure lookup).
2. Mode A design-time nudge — ships value with zero runtime cost, sets the honesty tone.
3. Replay harness + equivalence scorer (Mode B core) — reuses `run_eval` wholesale.
4. Ranker + console report card — the *confirmed* recommendation with the $ number.
5. Mode C aggregation to auto-surface the top-spend nodes worth step 3.
