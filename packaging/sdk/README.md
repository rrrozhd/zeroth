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

Version comparisons and schedules likewise require an explicit
`policy.min_success_rate`; omission or `null` yields `abstain`. Explicit zero
remains a caller choice. A policy pass can tolerate the configured cost growth
(10% by default), so a pass does not necessarily mean a saving.

New version results carry `claim_class="observed_comparison"` and
`method_version="observed-policy/1"`. New backtests carry
`claim_class="exploratory_model_experiment"` and
`method_version="observed-replay-policy/1"`. Both return `limitations` and use
`recommended_action="review_candidate"` for a pass. These fields are retained
in history and scheduled results. They do not authorize rollout, statistical,
causal or forecast claims. Older records load as `legacy_unclassified` /
`legacy_unversioned` with their original action and values; do not upgrade their
meaning based on the current SDK version.

Hosted version reports include `source_evidence.baseline` and `.candidate` with
a `stored-assertions/1` digest and selected execution/outcome record counts
(`stored-assertions/2` for window identities, or `/3` for charge ownership).
Changed selected inputs create a new retained revision even when the totals are
unchanged. These fingerprints are not signatures or proof of delivery completeness;
they cannot recover erased inputs. Historical reports without a binding return
an empty `source_evidence` map.

### Give each charge one owner

Use `cost_role="charge"` and a tenant-wide unique `charge_id` for the one execution
record that owns a physical charged attempt. Reuse that identity across capture
layers; do not create two monetary records for one call. Scope provider request IDs
to their provider/billing account when constructing charge IDs. Distinct billable
retries get distinct IDs, even when their inputs, model, timing and price match.
The SDK does not infer these identities from metadata or elapsed time.

```python
from zeroth.protocol import ExecutionEvent

charged_call = ExecutionEvent(
    workflow="invoice-processing", workflow_version="v2", run_id="run-1",
    step="extract", event_id="capture-1", cost_role="charge",
    charge_id="provider:account:request-1",
    cost_usd="0.02", cost_measurement="estimated",
)
parent_span = ExecutionEvent(
    workflow="invoice-processing", workflow_version="v2", run_id="run-1",
    step="workflow", event_id="parent-1", cost_role="summary",
)
```

A `summary` is structural telemetry: it cannot carry `charge_id` or a monetary
amount, including zero. Its token metadata is not priced again. Keep monetary
assertions on the charged calls, tool invocations and declared compute charges.
An intentional free charge is an explicit zero on a charge record; a run containing
only summaries has unknown cost. Missing cost on an owned charge also stays unknown.
Charge amounts must be nonnegative; credits/corrections are not a negative charge.

An exact execution retry is a duplicate. A different execution claiming an owned
charge ID is rejected, including concurrent attempts, and a rejected write does
not consume a retained event allowance. The rejected capture can be submitted as
a non-monetary summary if that accurately describes its role. Existing assertions
are immutable: changing a stored amount or owner conflicts. There is no charge
correction endpoint yet; do not invent a second charge ID to conceal a correction.

`legacy_unknown` is the default role, preserving existing caller-reported amounts
without inventing ownership. Version reports expose `charge_ownership` counts for
owned charges, summaries and unattributed records. `declared` describes supplied
identities in the selected records; `unverified` means that attribution is missing
or no charge record exists. Neither status proves a provider bill or that every
charge was captured. Existing `primary_for_rollup` and timing-based dedupe metadata
have no authority over money. Pair ownership with an independent source inventory
and provider reconciliation before claiming complete accounting.

### Reconcile a closed capture window

For delivery checks, assign `source_window_id` and an explicit, tenant-wide unique
`event_id` before sending each execution. Record every intended execution ID and every run's
technical terminal state in your own producer ledger. Build the inventory from
that ledger, including failed/cancelled runs and delivery failures. An inventory
derived from successful responses cannot reveal what was lost. Run IDs identify
whole runs within a workflow version; do not recycle them for another window.

For each run, supply its `execution_count` and `execution_ids_digest(ids)` from
`zeroth.protocol`. Supply a `SourceWindowInventory` for each side in the existing
`VersionComparisonRequest.source_windows` map:

```python
from zeroth.protocol import SourceWindowInventory, VersionComparisonRequest, execution_ids_digest

# This is a producer ledger entry, prepared independently of HTTP acknowledgements.
candidate_inventory = SourceWindowInventory.model_validate({
    "source_window_id": "candidate-batch-1",
    "opened_at": "2026-09-06T00:00:00Z",
    "closed_at": "2026-09-06T01:00:00Z",
    "runs": [{
        "run_id": "run-1",
        "terminal_state": "completed",
        "execution_count": 2,
        "execution_ids_digest": execution_ids_digest(["v2:run-1:model:1", "v2:run-1:tool:1"]),
    }],
})
# Prepare baseline_inventory from the baseline producer ledger in the same way.
# request = VersionComparisonRequest(
#     workflow="invoice-processing", baseline_version="v1", candidate_version="v2",
#     policy={"min_success_rate": 0.95},
#     source_windows={"baseline": baseline_inventory, "candidate": candidate_inventory},
# )
# report = client.compare_versions(request)
```

Both sides are required when using inventories. A window includes executions with
that exact ID, workflow version and tenant, whose asserted timestamp lies within
the inclusive interval. Timestamps must include an offset. The limit is 5,000 runs
and 50,000 expected executions per side. A larger received window returns a
mismatch with `scan_truncated=true`; its observed counts describe the bounded read,
not the full window. Existing schedules compare observed history and reject these
fixed inventories.

`source_delivery` reports inventory digests, expected/observed counts and mismatch
counts. Missing runs stay in the denominator. Missing or substituted steps make a
run's full cost unknown; an empty declared run also has unknown cost. A mismatch
forces abstention. A delayed event with its original in-window timestamp can
resolve a mismatch; an unexpected event after closure creates one. Rerunning with
changed evidence or an amended inventory creates a new retained report revision.
Keep the original inventories to reconstruct their report digests; Zeroth retains
digests and counts, not a second copy of the run/ID lists.

`matched` means received execution IDs agree with the supplied inventory. It does
not establish complete provider charges, inventory independence, mature business
outcomes, or a causal/statistical savings claim. `terminal_state` describes
application execution and does not set `accepted`. Without inventories,
`source_delivery` is empty and source completeness remains unverified.

For explicit HTTP clients, the event-ID digest is SHA-256 of compact UTF-8 JSON
containing distinct nonempty IDs sorted by UTF-8 bytes. Do not normalize Unicode or
escape non-ASCII. Node's ordinary string sort uses a different ordering:

```javascript
import { createHash } from "node:crypto";
const ids = ["v2:run-1:model:1", "v2:run-1:tool:1"];
const ordered = [...ids].sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)));
const digest = createHash("sha256").update(JSON.stringify(ordered), "utf8").digest("hex");
```

Outcome delivery retries must preserve the original value, provenance, metadata
and timestamps. Changed assertions return a conflict instead of `duplicate`;
they do not replace the original row. Explicit correction revisions and outcome
definition/maturity contracts remain acceptance work.

Workflow and version names belong to the authenticated tenant. Two tenants can
use the same names, and two workflows can both use `v1`. Reports join outcomes by
workflow, version and run; reusing a run ID under a different version does not
merge their results. Public SDK fields and event IDs are unchanged. Older evidence
keeps its original storage identity; ambiguous historical joins remain unresolved.
An outcome sent before its workflow version is registered returns an error and
can be retried after execution ingestion registers that version.

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
