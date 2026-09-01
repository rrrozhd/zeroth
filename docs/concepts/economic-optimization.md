# Economic debugger service

Status: implemented self-hostable evidence slice, authenticated SDK ingestion,
workflow-version decisions and schedules, plus provider-bill reconciliation,
2026-08-31. This page separates verified capability from intended paid behavior.
It is not a claim that a hosted Zeroth service is already available.

## One recognizable job

Zeroth explains the economics of an AI workflow before and after a change:

- where spend accumulated over time;
- which workflow version, step, user, customer, or cohort produced it;
- which groups fail or break the pipeline most often;
- what an accepted outcome actually costs; and
- whether a proposed change would reduce cost without violating outcome or
  governance constraints.

Zeroth does not need to replace LangGraph, an agent framework, an LLM gateway,
or an observability backend. Those systems execute, route, or trace work.
Zeroth joins economic evidence to outcomes and produces a defensible decision
about what to cut, change, or retain.

## Product and paywall boundary

The local debugger is the free trust layer. The subscription sells continuous
hosted operation and decision history rather than a basic cost dashboard.

| Free forever | Paid managed service, after activation |
|---|---|
| SDK, ingestion, and local verification | Managed ingestion and retained evidence |
| Single-team cost per successful outcome | Scheduled workflow-version decisions |
| Run and step economic timeline | Hosted simulations and post-change verification |
| Cohort and breakage queries | Decision history, notifications, and collaboration |
| Local caps and enforcement mechanics | Multi-team rollups, chargeback, SSO/RBAC, and retention |
| Local model-swap backtests | Signed proof-of-savings and compliance evidence bundles |

The land user may be a solo developer or small AI team that needs a recurring,
low-touch answer to “is this cheaper version safe to ship?” Organization finance
and governance controls are the expansion motion. Project API keys, retained
decisions, schedules, opt-in Trial/Solo/Team/Scale quotas, and a vendor-neutral
subscription projection are implemented. Merchant checkout, verified webhook
adaptation, production hosting, and service terms are not.

The billing projection accepts normalized events only after an external adapter
has verified their authenticity and resolved the tenant. It keeps exact retries
idempotent, rejects changed replays, and retains but does not apply older or
equal-time events. This prevents delayed webhook delivery from silently
restoring canceled access. Equal timestamps are intentionally first-wins and
must be repaired by periodic merchant-state reconciliation when ambiguous.

See the [provider bill reconciliation guide](../how-to/provider-bill-reconciliation.md)
for the import contract, closure states, and current limits.

## The evidence spine

Every supported ingestion path must converge on one queryable identity chain:

```text
tenant
  → workflow + workflow_version
    → run
      → step + attempt
        → cost + outcome + subject_id + dimensions + occurred_at
```

Required invariants:

- `tenant_id` is the security and account boundary; `subject_id` is the entity
  being analyzed, such as an end user, customer, account, or job.
- Dimensions are typed and bounded, not an unrestricted high-cardinality bag.
- Workflow version, event time, step identity, and attempt are first-class.
- Measured provider dollars and locally estimated dollars remain separate.
- Missing cost, outcome, or attribution is reported as incomplete evidence,
  never silently converted to zero.
- Success semantics are immutable per workflow version. A tenant Admin binds
  one terminal outcome type to an equality or numeric-threshold predicate;
  changing that rule requires a new workflow version.
- Retries and attempts remain individually visible so repeated work can be
  distinguished from one expensive first attempt.
- Events may be exported as OpenTelemetry GenAI semantic-convention data;
  Zeroth does not need to build a second generic tracing dashboard.

## Four event contracts currently exist

The first convergence slice extends the working instrumentation and plane
contracts with the same debugger identity. It does not introduce a fifth schema.

1. The standalone package under `packaging/sdk` defines execution, outcome,
   decision, schedule, and backtest contracts. Execution, outcome, backtest,
   comparison, schedule, and retained-history routes are served by the plane.
2. `zeroth.econ.instrumentation` is the absorbed Regulus instrumentation SDK.
   This is what the runtime actually emits.
3. `zeroth.econ.plane` owns the server-side ingestion schema. The
   instrumentation-to-plane path works today.
4. Native `Run` and `NodeAuditRecord` persistence owns runtime and signed audit
   evidence. Instrumentation also writes into this path.

The public SDK is adapted into the working plane evidence store rather than
becoming a parallel truth model. The in-repo route gap is closed; the wheel
remains blocked because the repository still proves no supported production
endpoint, managed provider credentials, checkout, or release operations.

## Free debugger queries

The self-hostable service exposes three questions over the converged spine:

1. **Timeline:** how did cost, successful outcomes, and failure tax change by
   workflow version and time window?
2. **Cohorts:** how do cost per successful outcome and failure rate differ by
   `subject_id` or typed dimensions?
3. **Breakage:** which versions and steps contain the most failed-run exposure
   and explicitly identified repeated-attempt spend?

The implemented routes are `GET /v1/debugger/timeline`,
`GET /v1/debugger/cohorts`, `GET /v1/debugger/breakage`, and
`GET /v1/debugger/report`. `POST /v1/debugger/outcome-definitions` creates the
immutable success contract; an exact replay is idempotent, while a changed rule
for the same workflow version is rejected. Reads require a tenant-bearing JWT
and a read-capable role; definition creation requires Admin. The UI remains an
open-source option and is not a prerequisite for the service.

## Evidence-gated version decisions

`POST /v1/decisions/compare` compares two exact workflow versions by measured
cost per accepted outcome. It returns an immutable decision artifact with a
pass, fail, or abstain verdict and a recommended action. The engine abstains
when the run count or outcome coverage is too low, cost is estimated or
unmeasured, outcomes are inferred, or the baseline has no accepted outcomes.
It does not convert missing evidence into zero or market a projected saving as
realized value.

`GET /v1/decisions` returns retained history. `POST` and `GET
/v1/decision-schedules` manage recurring comparisons, and the worker discovers
due tenants without a caller-supplied tenant override. Project API keys are
high-entropy, hashed at rest, revealed once, role-scoped, and revocable.
Hosted-mode quotas cover events, decision scans, active schedules, and schedule
frequency. Backtests additionally meter actual provider calls, reserve at most
four calls per case, and do not charge an exact immutable retry. Entitlement
enforcement is disabled by default for self-hosting.

## Backtests and honest attribution

Current model backtesting is real but bounded. `POST /v1/backtests` accepts
5–25 labeled, tool-free cases, replays the incumbent and one candidate, judges
correctness, checks the minimum success-rate constraint, and requires positive
projected model savings before it can pass. Raw inputs, expected outputs, and
instructions are not retained: history stores a keyed request digest plus the
result. Exact retries return that immutable result without another model call.

The isolated node replay abstains when pricing is unknown or when asked to
prove cost per business outcome or critical-error limits that its evidence
cannot establish. It does not yet simulate structural workflow
changes such as retry limits, optional verification steps, conditional paths,
context reduction, or redundant tool sequences.

Current waste attribution remains narrower than complete causality:

- loop re-execution can attribute dollars to a node;
- failed-run spend is confirmed at run level;
- repeated-attempt cost is priced only when the emitter supplies `attempt > 1`;
  it does not prove the step caused the final failure; and
- cache inefficiency is run-wide rather than step-attributed.

Therefore current output may rank opportunities, but must not claim complete
step-level causal attribution. Structural backtests come after the event spine
can identify versions and attempts reliably.

## Initial acceptance evidence

The first vertical slice has been exercised over 10,000 deterministic runs.
The verifier reconciled 5,000 successes, 5,000 failures, $100 measured spend,
$50 failed-run exposure, and $0.02 measured cost per successful outcome. Its
contract tests also cover multiple subjects, typed cohorts, steps, attempts,
measurement channels, idempotency, and tenant isolation. The service preserves:

- timeline, cohort, and breakage totals reconcile to the source fixture;
- tenant isolation is enforced on every read and write;
- measured and estimated spend never cross-contaminate;
- unknown or incomplete attribution is visible;
- replaying the same idempotent event cannot double-count spend; and
- the same dataset can be exported without requiring the web UI.

Decision-ready results additionally require an immutable outcome definition
for every workflow version in the selected window. Undefined versions remain
unresolved and are named in the diagnostic; Zeroth does not infer success from
positive numbers, booleans, strings, fraud flags, or other business values.

Queries deliberately scan at most 50,000 recent execution events per request.
This is a bounded single-team debugger, not an organization-scale warehouse.
Paid rollups should be pre-aggregated rather than increasing that request-time
limit indefinitely.

Only after this vertical slice should Zeroth extend the existing right-sizing
harness to retry, step-removal, or conditional-path simulations.

## Preserved repository surfaces

The graph runtime, service API, governance subsystems, integrations, Studio,
console, memory, RAG, sandbox, deployment code, and SDK prototype stay in the
repository. Nothing is deleted by this narrowing. Their product roles are:

- runtime and integrations produce economic and governance evidence;
- governance constrains and proves consequential changes;
- the console remains the optional open-source interface;
- the local `zeroth.optimization` façade exposes existing economic analytics;
  and
- legacy capabilities remain available without expanding the primary service.

## Adversarial review

The strongest objection is that free cost-per-outcome analytics may attract
users without converting them. That risk is real: standalone AI observability
has consolidated, gateways already optimize model price, and basic cost
visibility is widely free. The defensible bet is narrower: use economics as the
demand hook, then charge only when data crosses team, finance, security, or
audit boundaries. The simpler fallback is to keep the debugger entirely open
source and sell only managed hosting plus SSO/RBAC/retention if proof-of-savings
backtests do not create willingness to pay.
