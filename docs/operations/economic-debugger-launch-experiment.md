# Launch the closed-ledger demand experiment

> **Historical launch experiment (superseded 2026-08-31):** retained for its
> channel and measurement evidence. It is not the active SaaS launch plan. The
> current direction is a low-touch Trial/Solo/Team subscription whose immediate
> value is a bounded model-change backtest, followed by continuous production
> evidence and verification.

## Bottom line

Run one 14-day, asynchronous acquisition experiment around one promise: Zeroth
ties provider-billed AI dollars to workflow version and business outcome, or
identifies the dollars that cannot be reconciled. The initial user is an AI platform engineer
operating production workflows; the likely buyer is a Head of AI Platform or FinOps owner
responsible for allocation, chargeback, or evidence trust.

Use Hacker News as **one primary earned channel**, not as proof of the whole
market. Do not cross-post during the first 72 hours. This keeps the source of
inbound requests interpretable and limits the owner's work to an asynchronous
launch thread plus issue triage.

## Release gate

**Do not launch until** all of the following are true:

- the intended commit is on public `main`;
- the release candidate and manual promotion workflows pass;
- the same version is available from public PyPI;
- a clean machine can run `pip install "zeroth-core[regulus]"` and
  `zeroth-econ demo` without the repository checkout;
- the public issue template records highest artifact produced and discovery source; and
- the README still says that the managed service is not implemented.

The public package currently trails the local work. A post made before these
gates pass would send readers to an installation path that cannot reproduce the
claim.

## Audience and qualification

Write for the engineer who has all three of these conditions:

1. at least one production AI workflow with a terminal success or failure
   signal;
2. a usable provider cost export, initially OpenAI's Costs API; and
3. a recurring need to reconcile or allocate provider spend beyond a provider
   dashboard total.

A request is qualified when it describes a current closure, ownership,
outcome-attribution, or evidence-trust problem; identifies a plausible owner;
has usable provider evidence; and selects a budget of at least $500 per month.
Prefer a real closure report, but do not discard a strong request solely because
integration friction prevented it.

## Primary channel

Use Show HN only if the owner already has an **established Hacker News account**
and can participate in the thread. Hacker News currently limits Show HN access
for accounts unfamiliar with the community, and its official guidance requires
something people can try without a signup barrier. Do not create a replacement
account or try to route around that restriction. See the official
[Show HN guidance](https://news.ycombinator.com/showhn.html) and
[current eligibility notice](https://news.ycombinator.com/showlim).

If the account is not eligible, publish the GitHub/PyPI release but do not call
that an acquisition experiment. Select a different single channel in a later,
separately measured run.

### Submission

Use this title:

> Show HN: Zeroth – reconcile AI provider bills to workflow outcomes

Link directly to the GitHub repository, where the commands and limitations are
visible without an email or signup. Use this as the first comment, adjusting
only personal details that are not true:

> I built Zeroth after separating two questions that are often collapsed:
> what telemetry estimates a workflow cost, and whether those estimates close
> to the provider's billed total.
>
> The current open-source slice records workflow version, run, step, attempt,
> cohort, measured cost, and terminal outcome. It can then allocate a normalized
> provider statement through that evidence. Unmatched buckets, telemetry
> variance, and unresolved outcomes remain explicit instead of being forced
> into a complete-looking total.
>
> You can inspect the output without a server or signup:
>
> `pip install "zeroth-core[regulus]"`
>
> `zeroth-econ demo`
>
> The demo is synthetic and not proof of savings. The self-hosted debugger is
> usable now; managed hosting, automatic connectors, organization rollups, and
> billing are not implemented. I am testing whether teams with a real provider
> export and outcome signals need a managed closed-ledger service.
>
> If this matches a current operating problem, the repository links to a public
> design-partner form that asks only for aggregate bands—no invoices, prompts,
> traces, credentials, or customer identifiers.

Do not ask for upvotes, arrange comments, claim that competitors cannot perform
cost allocation, or describe historical exposure as causal waste or proven
savings. Answer technical questions directly and link to implemented contracts.

## Single call to action

Every launch surface ends with the same path:

```text
pip install → zeroth-econ demo → real instrumentation → real closure report
            → .github/ISSUE_TEMPLATE/economic-diagnostic-pilot.yml
```

Do not add a mailing list, calendar link, generic “contact sales” form, or a
second survey. The GitHub request is the one asynchronous qualification path.

## Measurement

Create one private row per inbound request with only:

| Field | Source |
|---|---|
| Campaign ID and submission time | Operator record |
| Thread URL | Hacker News submission |
| Discovery source | Required issue-form field |
| Highest artifact produced | Required issue-form field |
| Spend, budget, deployment, coverage, and problem bands | Existing issue-form fields |
| Qualified / not qualified plus reason code | Operator assessment |
| Time from submission to request | Timestamps |

GitHub traffic and PyPI download deltas may be recorded as directional reach.
**PyPI downloads are exposure, not activation**: mirrors, CI, retries, and bots
make them unsuitable as the denominator for product conversion. Do not add
hidden CLI telemetry to manufacture a cleaner funnel.

The authoritative outcomes after 14 days are:

- whether at least one request reports a real diagnostic or closure artifact;
- whether **one qualified request** asks for the managed closure or governance
  outcome with a plausible budget owner; and
- which setup step blocked otherwise qualified teams.

## Decision rules

- If one qualified organization independently requests the managed result,
  begin the smallest hosted-pilot readiness slice: tenant topology, credential
  issuance, retention/deletion, backups/restore, terms, and one provider import.
  Do not accept payment until the commercial pilot gates pass.
- If requests stop at the synthetic demo, improve real instrumentation and
  outcome-definition activation before adding organization features.
- If real closure reports appear but no one names a paid operating problem,
  keep the report free and test managed hosting plus SSO/RBAC/retention as the
  simpler paid offer.
- If there are no qualified requests, treat the result as evidence about this
  channel and message—not proof that the market does not exist. Do not build
  more product until a second independently measured channel is selected.

## Operating effort

Submit once, remain available for the first two hours, and spend at most 20
minutes per day for the next two days answering the thread. Triage GitHub
requests asynchronously with the existing qualification rules. No discovery
call is required to count demand; a confidential follow-up happens only after
a request qualifies and the organization agrees to the private data boundary.

## Adversarial review

The strongest objection is channel fit: Hacker News can produce developer
curiosity without reaching the finance or platform owner who can pay. That is
why comments, points, stars, downloads, and synthetic demos are not commercial
success. The experiment succeeds only on a qualified operating problem and
budget signal.

The simpler option is a GitHub release with no coordinated acquisition. It is
safer operationally but cannot distinguish weak demand from absent discovery.
One bounded Show HN run is therefore justified after—and only after—the package
is genuinely installable from the public release.
