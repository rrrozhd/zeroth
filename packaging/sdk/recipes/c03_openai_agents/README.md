# C03 — OpenAI Agents SDK (Python)

Capture every model call an existing OpenAI Agents SDK application makes,
through the SDK's documented `RunHooks` boundary, without replacing its trace
processors or changing what the application returns or raises. Status:
**conformance candidate, not an accepted support row**; see
`tests/acceptance/phase3_integrations/manifest.json`.

## Install

```bash
python -m pip install /path/to/zeroth/packaging/sdk -r requirements-current.txt
```

Tested pins: `requirements-floor.txt` (openai-agents 0.3.0, the first release
with `on_llm_start` / `on_llm_end`) and `requirements-current.txt`. No Zeroth
runtime, database or console is installed.

## The change

`app.py` is the application (a planner that hands off to a writer, a function
tool, a fallback policy, an application-level retry, an `asyncio.gather` fan-out).
It does not import Zeroth. `check.py` shows the instrumentation:

```python
from zeroth.instrumentation import Recorder
from zeroth.instrumentation.openai_agents import ZerothRunHooks
from zeroth.sdk import ZerothClient

recorder = Recorder(ZerothClient(api_key=KEY, base_url=URL), raise_on_error=False)

with recorder.run("support-agents", "2026.09", request_id).active() as run:
    with ZerothRunHooks() as hooks:
        result = await Runner.run(planner, question, hooks=hooks)
    run.tool_charge("search", tool="web_search", cost_usd=Decimal("0.005"))
    run.summary("completed", metadata=hooks.aggregates())
    run.outcome(customer_accepted, maturity="final")
```

- **Physical calls.** Each `on_llm_end` is one charge from `ModelResponse.usage`
  (cached tokens subtracted from input, `request_id` retained where the SDK
  provides it), named after the agent (`planner`, `planner#2`, `writer`).
- **Aggregates.** Agent runs, turns and handoffs are counted in
  `hooks.aggregates()` for the run summary (money-free); they are never charged.
- **Failures.** The SDK fires no hook for a model call that raises, so leaving
  the `with ZerothRunHooks()` block charges every call still pending as an
  unmeasured attempt with the exception's name; the exception propagates.
- **Provider wrappers.** If the `AsyncOpenAI` client behind the model provider
  is also instrumented with `instrument_openai`, the adapter charges the HTTP
  call and the hooks skip it; totals do not change.
- **Identity.** A model given by name runs on the SDK's default OpenAI provider.
  A model object declares `provider` or a `provider/model` name (LiteLLM style);
  nothing is inferred from response shapes. Providers other than `openai` and
  `anthropic`, and unlisted models, stay unmeasured.
- **Missing usage.** A response the SDK reports with no usage (`requests == 0`
  or zero tokens) is an unmeasured charge, never zero.
- **Tracing.** Register your own processors with `add_trace_processor` as
  before; the hooks never call `set_trace_processors`.

## Verified here

`tests/acceptance/phase3_integrations/test_c03_openai_agents.py`: the frozen
reference workload reconciles exactly through the agents for two versions;
re-delivery is a no-op; a handoff plus tool run charges each model call once;
a raising model call is charged once; hooks-only and hooks-plus-`instrument_openai`
ledgers are identical over the C01 replay; a caller-added trace processor
keeps receiving spans; clean installs at both pins.

## Not verified

No live provider call. `Runner.run_streamed` is not exercised. The TypeScript
Agents SDK, LiteLLM-backed models and hosted tool charges are out of scope.
Independent reviewer execution is pending.
