# Run the economic debugger commercial pilot

> **Historical validation plan (superseded 2026-08-31):** retained as research
> evidence, not the active go-to-market plan. The owner subsequently selected a
> self-serve recurring SaaS centered on economic change control, with bounded
> model-change backtests as the first-value event. Do not use the closed-ledger
> pilot or its $500–$1,500 pricing experiment as the current product offer.

## Bottom line

Use the free self-hosted diagnostic to find production teams with an
organization-level reconciliation problem. The paid-product hypothesis is one
plain promise: **close the AI spend ledger by tying provider-billed dollars to
the team, workflow, and outcome that incurred them—or marking the variance as
unreconciled.** The repository now contains a normalized provider-bill import
and allocation report, but it has not closed a real customer export and the
managed organization boundary is not implemented. Do not sell the debugger
report—or this unvalidated primitive—as a finished hosted product.

## Offer boundary

| Layer | User receives | Commercial role |
|---|---|---|
| Free, self-hosted | Evidence ingestion, cost per successful outcome, timeline, cohort analysis, failed-run exposure, one bounded diagnostic | Trust, activation, and proof that the data can answer an operating question |
| Paid hypothesis | Provider-invoice reconciliation, cross-team ownership and rollups, chargeback/showback exports, retention, organization access controls, and signed change evidence | Organization economics control |

The existing reconciliation screen calibrates ground-truth costs against
estimates. It is not provider-invoice reconciliation. The new provider-bill API
imports immutable billed-cost buckets and allocates them through measured
workflow/outcome evidence while preserving variance. It is the paid-product
prototype, not proof of willingness to pay. The existing backtest supports a
bounded model-swap case; it does not yet prove savings for structural workflow
changes.

## Low-touch activation loop

1. A developer can run `zeroth-econ demo` to inspect the product's bounded
   output without a server or UI. It does not count as a first diagnostic,
   production validation, or commercial evidence.
2. The developer installs the backend without the UI and instruments one real
   workflow with `InstrumentationClient.authenticated`. The setup check uses
   confirmed execution and outcome delivery so rejected evidence cannot look
   like activation.
3. An Admin defines success for the exact workflow version; the definition is
   immutable and content-digested.
4. The team collects 7–14 days of representative execution and outcome evidence.
5. The developer runs `zeroth-econ diagnose` and keeps the artifact locally.
6. For OpenAI, an Admin converts one complete local Costs API page with
   `zeroth-econ normalize-openai-costs`, then runs `zeroth-econ reconcile`.
   Other providers use the normalized contract until a real export justifies an
   adapter. Neither the provider data nor the report is uploaded.
7. If the closure report exposes a current operating problem, the team opens the
   [public design-partner request](https://github.com/rrrozhd/zeroth/issues/new?template=economic-diagnostic-pilot.yml)
   with aggregate bands and problem descriptions only.
8. Zeroth qualifies the request asynchronously. A pilot advances only when the
   organization names a current provider-bill closure problem, has usable cost
   evidence, and identifies a plausible budget owner.

Do not request raw prompts, responses, traces, credentials, `subject_id`
values, customer names, proprietary workflow names, or unredacted reports in a
public issue. Move any later confidential exchange to an approved private
channel with explicit retention and deletion terms.

## Qualification experiment

The following are hypotheses to test, not validated market facts or product
requirements:

- a real provider export or Costs API result is available;
- a recurring manual, disputed, or missing reconciliation process exists;
- the primary gap is reconciliation, allocation, outcome attribution, or
  evidence trust—not “no current provider-bill closure problem”;
- at least 100 resolved runs in the selected window;
- at least 90% outcome coverage;
- no undefined workflow versions in the report window;
- a closure report shows variance, unresolved outcomes, or an ownership gap; and
- an explicit monthly budget band and organization owner exist.

Do not reject a strong buyer solely for missing a numerical threshold. The
purpose is to distinguish a real recurring operating decision from curiosity
about a dashboard.

Track this funnel without collecting production content:

```text
demo → real instrumentation → first event → first resolved outcome → first diagnostic
        → first provider-bill closure report → design-partner request → paid pilot
```

Record counts and elapsed time between stages. The decisive conversion is not
repository interest or report generation; it is an organization asking to pay
to close a reconciliation or governance gap.

## Pricing and payment gate

Use the issue form's budget bands to learn willingness to pay before publishing
a permanent SKU. A reasonable founding-pilot test is $500–$1,500 per month,
month to month, for one organization and a deliberately limited integration
scope. That range is a pricing experiment, not evidence of market acceptance.
Do not accept payment or promise a managed SLA until all of these exist:

- tenant isolation verified for the actual hosted topology;
- encrypted backups plus a witnessed restore procedure;
- retention and deletion controls;
- authentication and organization authorization appropriate to the buyer;
- service terms, privacy terms, support boundary, and incident contact; and
- at least one real provider-billing import with a reconciliation audit trail.

No bespoke provider or workflow integration belongs in the founding offer
unless it is reusable and separately scoped. The initial paid deliverable must
remain the closed-ledger result, not general consulting.

## Build trigger and stop criteria

The normalized reconciliation primitive was implemented to test the hypothesis.
Do not build provider credential storage, multiple bespoke adapters, or the
entire organization shell until at least one qualified organization requests
this closure result without being led to the answer, or three qualified
organizations independently rank it as their first paid need.

Pause or change the wedge if either experiment fails:

- 20 inbound requests produce fewer than two real diagnostics or closure
  reports; activation is the problem, so simplify instrumentation and the
  diagnostic path. Do not substitute PyPI downloads for this explicit evidence.
- 20 real closure reports produce no qualified paid request; the report is
  useful but not a buying trigger, so test managed hosting plus
  SSO/RBAC/retention as the simpler paid offer.

Use the [launch experiment](economic-debugger-launch-experiment.md) for the
single-channel copy, attribution fields, release gate, and 14-day decision rule.

## Adversarial review

The strongest objection is that provider clouds, gateways, and observability
vendors can add cost allocation. Zeroth only has a defensible position if it
reconciles billed money through workflow execution to business outcome and
preserves evidence quality and ownership boundaries. Cost charts alone are not
a moat.

The key unknowns are whether teams have reliable outcome signals, whether
finance will trust developer-generated evidence, and whether invoice variance
is painful enough to fund another system. The safer fallback is to keep the
debugger open source and sell managed hosting, retention, and organization
access controls rather than claiming unique savings intelligence.
