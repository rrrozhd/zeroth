# Phase 3 shared capture contract

Every recipe maps its framework onto `zeroth.instrumentation.capture` and nothing
else. The sold wire contract (`zeroth.protocol.ExecutionEvent` / `OutcomeEvent` to
`POST /v1/executions` and `/v1/outcomes`) and the server's ingestion, pricing and
schemas are unchanged by Phase 3.

## Identity

| Concept | Field | Rule |
| --- | --- | --- |
| Workflow, version | `workflow`, `workflow_version` | Supplied by the customer; the economics-relevant version is the customer's own. |
| Run | `run_id` | One end-to-end application request or agent run. |
| Step | `step` | Unique per physical call within a run. Repeated invocations of the same node get `name`, `name#2`, `name#3` from `Run.step()`. |
| Attempt | `attempt` | Retries and fallbacks of the same step count 1, 2, 3; each attempt is charged, on whichever model served it. |
| Event | `event_id` | `workflow:version:run:step:attempt`; the summary is `...:summary:1`. Redelivery of the same event is a `duplicate`, never a second row. |
| Charge | `charge_id` | `charge:` + event id. A physical call owns exactly one charge; a different event claiming it is refused (422). |

## Physical calls and aggregates

- `Run.charge` = one physical model call: `cost_role="charge"`, `model_version` = the
  provider's model id, `metadata.charge_kind="model"`.
- `Run.tool_charge` = one priced tool call: `cost_role="charge"`, `metadata.charge_kind="tool"`,
  amount asserted by the caller (`measured` or `estimated`).
- `Run.summary` = the run aggregate: `cost_role="summary"`, which cannot carry money.
  Framework-level totals (agent run, crew, multi-step generation) are summaries, so a
  framework hook and a provider wrapper capturing the same call cannot double count:
  only the provider-level physical call carries an amount.

## Pricing

- `cost_measurement="estimated"` from the pinned rate card (`zeroth.instrumentation.rate_card`,
  version recorded in `metadata.rate_card_version`) for the listed models only.
- Usage split retained under `metadata.usage`: `input_tokens` (excluding cached tokens),
  `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`.
  OpenAI adapters subtract `cached_tokens` from `prompt_tokens`; Anthropic usage is already split.
- Missing usage (`metadata.pricing="missing_usage"`) or an unlisted model
  (`"unknown_model"`) stays `unmeasured` with `cost_usd=null`. Never zero.
- Streaming: usage is final only when the stream completes and reports usage; an aborted
  stream or a provider exception is a charged, unmeasured attempt.
- An OpenAI-compatible base URL is not OpenAI: the provider is `unknown` and the model is
  priced only if the recipe declares the routed model explicitly.

## Outcomes

- `Run.outcome(accepted, maturity, occurred_at)` is the customer's truth; recipes never infer it.
- `provisional` may precede `final`; the latest `occurred_at` is the run's current outcome.
- Missing outcomes are missing, not failures.

## Delivery

- Synchronous HTTP per event. A failed delivery is retained in `Recorder.lost`; adapters
  built with `raise_on_error=False` never alter the application's outputs, exceptions or
  cancellation. Duplicate delivery is safe by identity.

## Frozen reference workload

`workload.py` defines nine families over two versions; `expected_ledger.json` holds the
totals in 1e-8 USD units, derived once by exact fraction arithmetic from the workload and
the card rates. `ledger.py` reads only stored rows. `manifest.json` pins their digests.
