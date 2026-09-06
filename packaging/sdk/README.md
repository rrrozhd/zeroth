# zeroth-sdk

The lean Python client and instrumentation boundary for Zeroth's economic
debugger. It records workflow execution and outcome evidence, retrieves
economic analytics, compares workflow versions, and requests bounded model
backtests without installing the Zeroth runtime or web console.

## Installation status

No SDK release is currently available on either index. The commands below are
the supported install paths after their corresponding release has been
published.

From PyPI, after the production release gate is opened:

```bash
python -m pip install zeroth-sdk
```

From TestPyPI, for the current pinned development candidate:

```bash
python -m pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  zeroth-sdk==0.0.0.dev0
```

The explicit version prevents this validation command from silently selecting
a different SDK release. PyPI remains the fallback index for the SDK's public
runtime dependencies.

## Publishing status

PyPI and TestPyPI use credential-free Trusted Publishing from
`.github/workflows/release-zeroth-sdk.yml`. The manual workflow builds one
sdist/wheel pair, checks its metadata, smoke-installs the wheel, and promotes
that same artifact through the registry-specific `testpypi` or `pypi` GitHub
environment. No long-lived registry token is stored in the repository.

Production publishing remains intentionally blocked. The client routes have
authenticated server implementations, retrieval methods, and client-to-server
tests, but there is no supported production hosted endpoint, managed provider
credential path, or completed WorkOS/Paddle production transaction. The
`0.0.0.dev0` version and `tool.zeroth.release.publish = false` marker encode
that hold. A PyPI run fails closed until that marker is deliberately changed;
the `pypi` environment also requires approval from `rrrozhd`.

The SDK contains only public wire contracts, HTTP client operations, and the
instrumentation namespace. It does not ship the Zeroth runtime, service,
economic plane, database migrations, or web console.

Execution cost is unknown when `cost_usd` is omitted or `None`; the wire value is
`null` with `cost_measurement="unmeasured"`. A supplied amount, including explicit
zero, is treated as caller-reported measured cost for existing-client compatibility
unless you explicitly mark it `estimated`. Use `estimated` for rate-card estimates.
Measured here is the caller's assertion, not proof of a provider invoice or of
complete run costs. Measured/estimated costs require an amount, and unmeasured
costs cannot include one. Unknown costs prevent a complete-cost version approval.

Hosted backtests require an explicit `constraints.min_success_rate`. Omitting it
returns an abstention before provider execution or credit reservation. A pass
means the declared requirements were met on those cases; it is not a statistical
guarantee for future application traffic. A zero floor explicitly permits zero
observed success, so choose a requirement that reflects your actual task.

New hosted backtests price each model's observed replay input/output usage at the
retained input/output rates (`cost_basis="rate_card_from_observed_usage"`). The
result includes `incumbent_replay_cost_usd`, `candidate_replay_cost_usd`, separate
`judge_cost_usd`, `pricing_snapshot`, and `usage_by_role`. Dollar values serialize
as decimal strings. `savings_pct` compares candidate to incumbent replay totals;
negative values mean the candidate costs more on those cases. Judging expense is
the cost of evaluating the experiment and is excluded from that comparison.

These are text rate-card estimates, not invoices or realized production savings.
Cache pricing, discounts, provider-hosted tools, downstream work and provider
internal retry charges are outside this estimate. Missing, inconsistent or
failed-call usage causes abstention. Call credits count observed adapter
invocations; unseen provider retries still require provider reconciliation.
Historical reports without this evidence remain readable with an `unavailable`
cost basis; their old savings projections are not retroactively validated.

This corrects older SDK defaults that serialized omitted cost as measured zero.
Existing records and explicit-cost callers remain readable; old-client default
zeros cannot be distinguished from intentional zeros and are not retroactively
certified as complete evidence. No stored records are rewritten by this change.

```python
from zeroth.protocol import (
    BacktestCase,
    BacktestRequest,
    EconomicConstraints,
    ExecutionEvent,
    OutcomeEvent,
    VersionComparisonRequest,
)
from zeroth.sdk import ZerothClient

client = ZerothClient(api_key="zth_...", backtest_timeout=120.0)
client.record_execution(
    ExecutionEvent(
        workflow="invoice-processing",
        workflow_version="v7",
        run_id="run-1",
        step="extract",
        cost_usd="0.031",
    )
)
client.record_outcome(
    OutcomeEvent(
        workflow="invoice-processing",
        workflow_version="v7",
        run_id="run-1",
        accepted=True,
    )
)
decision = client.compare_versions(
    VersionComparisonRequest(
        workflow="invoice-processing",
        baseline_version="v6",
        candidate_version="v7",
    )
)

backtest = client.create_backtest(
    BacktestRequest(
        workflow="invoice-processing",
        baseline_version="v7",
        node_id="extract",
        incumbent_model="openai/gpt-5-mini",
        instruction="Extract the invoice fields.",
        candidate={"model": "openai/gpt-5-nano"},
        cases=[
            BacktestCase(id=f"invoice-{index}", input={"text": text}, expected={"total": total})
            for index, (text, total) in enumerate(
                [
                    ("Invoice total: 12.50", "12.50"),
                    ("Amount due: 44.00", "44.00"),
                    ("Total USD 8.75", "8.75"),
                    ("Please pay $103.20", "103.20"),
                    ("Balance: 0.99", "0.99"),
                ],
                start=1,
            )
        ],
        constraints=EconomicConstraints(min_success_rate=0.95),
    )
)
```

Production backtests require 5–25 labeled, tool-free cases. The five-case
example is the shortest real first-value request; replace its synthetic labels
with representative cases before using its verdict. Backtests may make several
provider calls, so they use a separate 120-second timeout by default while
ordinary ingestion keeps the client's normal 10-second default. Set
`backtest_timeout` explicitly for slower providers. Inputs and expected outputs
are used ephemerally. The service retains only a keyed request digest and the
result, so an exact retry returns the immutable prior decision without spending
provider-call credits again.
