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
