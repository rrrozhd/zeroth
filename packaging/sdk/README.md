# zeroth-sdk

The Python client for a Zeroth deployment. It records workflow execution and
outcome evidence, retrieves economic analytics, compares workflow versions,
and requests backtests and retained probabilistic model-migration decisions
without installing `zeroth-platform` or `zeroth-console`.

## Installation

Install the public package from PyPI:

```bash
python -m pip install zeroth-sdk
```

For release-candidate verification against TestPyPI:

```bash
python -m pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  zeroth-sdk==0.1.0
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

The `0.1.0` release supports self-hosted Zeroth deployments. Pass the deployment's
URL explicitly with `base_url`; there is no implicit public Zeroth Cloud endpoint.
The `pypi` environment requires an approval from `rrrozhd`, and the release
workflow fails closed unless `tool.zeroth.release.publish = true`.

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

client = ZerothClient(
    api_key="zth_...",
    base_url="https://zeroth.example.com",
    backtest_timeout=120.0,
)
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

The managed model-migration route accepts paired incumbent/candidate numeric
observations, observed demand periods, customer-owned chance constraints and
CVaR limits, and forecast-versus-observed calibration history. Use
`create_model_migration_decision(...)` to submit the typed
`ProbabilisticMigrationRequest` and `list_model_migration_decisions(...)` to
read immutable history. Cloud derives calibration readiness from the supplied
history instead of trusting a client readiness flag. Probabilistic decisions
use the client's configurable `backtest_timeout` because scenario evaluation
may outlive an ordinary ingestion request. See the
[model-migration guide](https://rrrozhd.github.io/zeroth/how-to/probabilistic-model-migration/)
for the complete request.

For the managed loop, `refresh_model_migration_decision(...)` harvests current
tenant telemetry or an explicit case-level backtest artifact;
`create_probabilistic_decision_schedule(...)` stores a selector and rebuilds
evidence on each run. `create_randomized_rollout(...)` creates the study,
`assign_randomized_rollout(...)` returns a sticky subject assignment before
execution, and `verify_randomized_rollout(...)` estimates post-assignment effects
and appends calibration observations. Aggregate-only backtest records are not
expanded into synthetic cases and therefore produce an evidence abstention.
