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
`method_version="observed-policy/3"`. New backtests carry
`claim_class="exploratory_model_experiment"` and
`method_version="observed-replay-policy/1"`. Both return `limitations` and use
`recommended_action="review_candidate"` for a pass. These fields are retained
in history and scheduled results. They do not authorize rollout, statistical,
causal or forecast claims. Older records load as `legacy_unclassified` /
`legacy_unversioned` with their original action and values; do not upgrade their
meaning based on the current SDK version.

Hosted version reports include `source_evidence.baseline` and `.candidate` with
a `stored-assertions/1` digest and selected execution/outcome record counts
(`stored-assertions/2` for window identities, `/3` for charge ownership, or `/4`
when selected outcomes declare maturity; `/5` binds selected charge-cost revisions).
Changed selected inputs create a new retained revision even when the totals are
unchanged. These fingerprints are not signatures or proof of delivery completeness;
they cannot recover erased inputs. Historical reports without a binding return
an empty `source_evidence` map.

New reports also carry `calculation_inputs` (`run-economics/1`). Its baseline and
candidate rows contain exact cost, cost provenance, accepted/unresolved outcome,
outcome provenance and a `runs` multiplicity. Identical tuples are grouped without
copying source identifiers or raw payloads. These inputs stay with the retained
decision when later evidence changes a fresh comparison.

To check an original report's measured total independently:

```python
from fractions import Fraction

decision_id = "<retained decision ID>"
report = next(row for row in client.list_decisions()
              if row["decision_id"] == decision_id)
inputs = report.get("calculation_inputs")
if inputs is None:
    raise ValueError("This historical report has no retained calculation inputs")
measured = sum(
    (Fraction(row["cost_usd"]) * row["runs"] for row in inputs["baseline"]
     if row["cost_measurement"] == "measured"), Fraction(0)
)
assert measured == Fraction(report["baseline"]["measured_cost_usd"])
```

Decimal strings may use exponent notation, such as `1E-8`; preserve exact values
instead of converting money to floats. A row represents whole-run economics: one
unknown required charge makes that run's cost unknown. Multiplicity must be included
in every total and denominator. See the repository's economic-optimization guide
for the complete calculation rules. These inputs reproduce arithmetic, not source
truth or invoice reconciliation. They remain with retained reports after source
erasure and do not recover erased identifiers. Existing retention obligations apply.

### Declare what an accepted outcome means

Before comparing stored workflow versions or scheduling a comparison, an Admin
registers the success rule for each version:

```python
from zeroth.protocol import OutcomeDefinition
from zeroth.sdk import ZerothClient

client = ZerothClient(api_key="<Admin project key>")
for version in ("v1", "v2"):
    client.create_outcome_definition(OutcomeDefinition(
        workflow_id="invoice-processing", workflow_version=version,
        outcome_type="accepted", operator="equals", target=True,
    ))
```

The SDK's boolean `OutcomeEvent.accepted` is the reported observation; this rule
states that `True` counts as success. A definition is immutable within a workflow
version. Exact creation retries return the existing definition, including races;
changing its rule returns a conflict. Use a new workflow version for changed
semantics. The existing HTTP paths are POST/GET
`/v1/debugger/outcome-definitions`; Admin can write and read roles can inspect.
Numeric predicates apply to raw numeric observations through the generic outcome
API; setting an SDK `score` does not replace its boolean accepted observation.

Stored comparisons, debugger and provider allocation use the same typed predicate.
Missing definitions or an outcome-type mismatch leave the labels unresolved.
Comparisons require identical declared rules across versions and abstain when rules
are unavailable or incompatible, even if observed costs are lower. Different rules
that happen to classify today's examples identically are not interchangeable.
Missing observations stay unresolved; an explicitly reported empty string remains
a value. An uninterpretable latest label cannot revive an older success.

Reports retain `outcome_semantics` by side: status, the immutable definition digest,
and a rule digest excluding the workflow version. These fields participate in the
existing retained-decision identity. Adding definitions creates a new decision
revision without rewriting old history or changing execution/outcome fingerprints.
Keep the source definitions for reconstruction; a digest cannot recover erased
values. Identical predicates do not prove mature labels, equivalent populations,
independent business truth, or causal savings. Old retained reports have an empty
semantics map and keep their original meaning. Existing callers must register their
rules before newly computed stored comparisons can pass.

### Declare when an outcome is final

`OutcomeEvent.maturity` defaults to `unknown`. Set `final` only when your business
process has resolved the observation under its declared rule. An execution
finishing, a measured value, or metadata saying "completed" does not establish
business finality. Only final, interpretable observations resolve a run as success
or failure. Provenance remains separate: a final inferred label is still inferred.

Use `provisional` while a result can still change and `withdrawn` to retract the
current observation. `accepted` may be `None` for unresolved states; final requires
a boolean, and withdrawn requires `None`. Existing events without maturity remain
unknown. Do not bulk relabel historical data merely to make a comparison pass.

`occurred_at` is the source time of the assertion or revision. Non-unknown maturity
requires a timezone-aware timestamp; SDK construction defaults it to the current
UTC time. Keep the constructed event and its timestamp for exact retries. For a
correction, append another event with the same workflow/version/run/type and a
later authoritative assertion time. Keep the original business-event time
separately when it differs. Changed content at the same identity conflicts.

A newer provisional or withdrawn assertion suppresses an earlier final result.
An older assertion delivered late is retained but cannot replace the newer state.
Future assertions are excluded from current reports. Corrections create new
retained report revisions; existing reports keep their original values. Source
clock correctness and business truth still require independent evidence. Correct
charge amounts through [charge-cost revisions](#correct-the-cost-of-an-existing-charge),
separately from outcome assertions. A complete historical snapshot API remains unavailable.

### Give each charge one owner

Use `cost_role="charge"` and a tenant-wide unique `charge_id` for the one execution
record that owns a physical charged attempt. Reuse that identity across capture
layers; do not create two monetary records for one call. Scope provider request IDs
to their provider/billing account when constructing charge IDs. Distinct billable
retries get distinct IDs, even when their inputs, model, timing and price match.
The SDK does not infer these identities from metadata or elapsed time.

Original execution amounts use the same 18,8 storage range as charge revisions:
at most ten integer digits and eight fractional digits. Unsupported precision or
range is rejected before storage. For `cost_role="charge"`, supply decimal strings,
Python Decimal objects or integers; floats can round before validation and are
rejected. Representable legacy-role floats remain accepted, with unverified charge
ownership. New SQLite storage preserves exact decimal text; PostgreSQL retains
Numeric(18,8). This does not recover digits already lost in historical SQLite rows.


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
are immutable: changing a stored amount or owner conflicts. Use a charge-cost
revision tied to that existing owner for corrections.

The stored attempt number for new executions preserves `attempt` (1 through 1000).
Larger new values are rejected with HTTP 422. Earlier SDK ingestion kept this
number in metadata but stored the canonical attempt as one, so historical retry
breakdowns can undercount repeated-attempt spend. Exact retries preserve those old
records; they do not repair historical classification. Changed raw attempt values
still conflict. Do not treat old retry breakdowns as independently reconciled.

### Delivery and recovery

`ZerothClient` sends each record synchronously. It adds no buffer, automatic retry
or durable outbox. A successful response reports `inserted` or `duplicate`.
HTTP and connection errors propagate to the caller; inspect an HTTP error's
`response` for the server's status and explanation. A connection error or lost
response does not prove that the server rejected the write.

Keep source IDs and payloads independently of acknowledgements. Retry the same
record unchanged after an uncertain response; do not allocate a new execution or
charge ID for telemetry retransmission. A genuinely new charged provider attempt
gets its own identity. Concurrent identical delivery is reconciled against the
complete immutable payload, while a different execution claiming the same charge
remains a conflict.

Abrupt process exit can leave source records unsent. A producer-owned inventory
keeps that loss visible; replay from the retained source can resolve the mismatch.
Zeroth does not reconstruct unsent records for the caller. This explicit SDK
delivery contract does not certify buffered runtime adapters or framework capture.

`legacy_unknown` is the default role, preserving existing caller-reported amounts
without inventing ownership. Version reports expose `charge_ownership` counts for
owned charges, summaries and unattributed records. `declared` describes supplied
identities in the selected records; `unverified` means that attribution is missing
or no charge record exists. Neither status proves a provider bill or that every
charge was captured. Existing `primary_for_rollup` and timing-based dedupe metadata
have no authority over money. Pair ownership with an independent source inventory
and provider reconciliation before claiming complete accounting.

### Correct the cost of an existing charge

Append a `ChargeCostRevision` when the authoritative source revises the cost of
an already recorded charge. The original execution and its physical owner remain
immutable; a revision does not create another attempt or run.

```python
from datetime import UTC, datetime
from zeroth.protocol import ChargeCostRevision

client.record_execution(charged_call)
revision = ChargeCostRevision(
    charge_id="provider:account:request-1",
    asserted_at=datetime.now(UTC),
    token_cost_usd="0.0275",
    cost_measurement="measured",
    reason="Corrected provider charge",
)
client.record_charge_cost_revision(revision)
history = client.list_charge_cost_revisions(revision.charge_id)
```

The charge must already exist in the authenticated tenant. Supply the complete
replacement token/tool/compute cost assertion; omitted components do not carry
forward. Amounts are nonnegative USD with at most eight decimal places and ten
integer digits. Send fractional amounts as decimal strings (or Python Decimal
objects), since JSON numbers can round before validation. Integral amounts may
also use integers. Explicit measured zero records a full refund. To withdraw an
amount, use `cost_measurement="unmeasured"` with all amounts omitted. Use
`estimated` for estimates; a revision does not establish invoice truth.

The aware source `asserted_at` must be later than the original execution timestamp.
Retain that timestamp and payload for exact retries. Changed assertions at the
same charge/time conflict. The latest non-future revision governs; late delivery
of an older assertion cannot replace it. A correction applies to the original
run and execution period, rather than describing receipt-time cash flow.

POST `/v1/charge-cost-revisions` uses the existing Admin/Analyst write roles and
events allowance. Exact duplicates and rejected writes consume no retained event
units. GET returns the latest 100 assertions, newest first, including future source
assertions; `limit` permits up to 1000. Read roles can inspect history.

Comparisons bind revision counts and assertions in `stored-assertions/5`; old
reports remain immutable. Deleting source executions also erases their cost
revisions. Execution-inventory matching does not prove delivery of these revisions
or business outcomes. Frozen snapshots, independent source truth and account-level
credits remain separate acceptance work.

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
they do not replace the original row. Use the documented
[outcome revisions](#declare-when-an-outcome-is-final) and
[charge-cost revisions](#correct-the-cost-of-an-existing-charge) for corrections.
These contracts do not establish source completeness or independently verified
business outcomes.

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
        maturity="final",  # Only after the business process resolves this observation.
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
