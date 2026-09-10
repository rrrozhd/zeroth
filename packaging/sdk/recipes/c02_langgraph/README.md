# C02 — LangGraph / LangChain Python applications

Capture every chat-model call an existing LangGraph or LangChain application
makes, through the framework's own callback interface, without changing the
graph, its outputs, its interrupts or its existing tracing. Status:
**conformance candidate, not an accepted support row**; see
`tests/acceptance/phase3_integrations/manifest.json`.

## Install

```bash
python -m pip install /path/to/zeroth/packaging/sdk -r requirements-current.txt
```

Tested pins: `requirements-floor.txt` (langchain 1.0.0, langgraph 1.0.0,
langchain-openai 1.0.0) and `requirements-current.txt`. No Zeroth runtime,
database or console is installed.

## The change

`app.py` is the application (graphs with fan-out through `Send`, a merge, an
approval `interrupt`, a subgraph and a final step; a fallback chain; an
application-level retry). It does not import Zeroth. `check.py` shows the
instrumentation: one callback handler per invocation, a run per request, tool
charges you price yourself, and the outcome you own.

```python
from zeroth.instrumentation import Recorder
from zeroth.instrumentation.langchain import ZerothCallbackHandler
from zeroth.sdk import ZerothClient

recorder = Recorder(ZerothClient(api_key=KEY, base_url=URL), raise_on_error=False)

with recorder.run("support-graph", "2026.09", request_id).active() as run:
    result = graph.invoke(state, {"callbacks": [ZerothCallbackHandler()],
                                  "configurable": {"thread_id": request_id}})
    run.tool_charge("search", tool="web_search", cost_usd=Decimal("0.005"))
    run.summary("completed")
    run.outcome(customer_accepted, maturity="final")
```

- **Physical calls.** Each chat-model call the framework reports becomes one
  charge from the message's `usage_metadata`; provider and model come from the
  response metadata (`model_provider`, `model_name`), so `ChatOpenAI`,
  `ChatAnthropic` and fakes behave alike. The step is the LangGraph node name
  (`branch`, `branch#2` for parallel branches), otherwise `chat_model`.
- **Aggregates.** Chains, graphs and nodes are never charged. The run summary
  and outcome are recorded once by the application.
- **Provider wrappers.** If the same client is also instrumented with
  `instrument_openai`, the provider adapter charges the HTTP call and the
  handler skips it (a context-local scope marks the call). Totals do not change.
- **Interrupt and resume.** Charges follow physical calls: nodes completed
  before an `interrupt` are checkpointed and not re-run on resume. A node that
  calls a model and then interrupts re-executes on resume and is charged again,
  because the provider ran again; put the interrupt in its own node.
- **Streaming.** With `ChatOpenAI(stream_usage=True)` the final chunk carries
  usage and the call is priced; a streamed call without usage stays unmeasured.
- **Fallbacks and errors.** `with_fallbacks` records the failed attempt as an
  unmeasured charge with the error name, then the fallback's charge. A raising
  call is one unmeasured charge; the exception propagates unchanged.
- **Retries.** A LangChain retry (`with_retry`) or an application retry is a
  new step occurrence; provider-internal retries are visible only through the
  provider adapter.
- **Pricing.** Estimated from the pinned rate card for providers `openai` and
  `anthropic` and the listed models; a proxied `ChatOpenAI` still reports
  provider `openai`, so pass `priced_providers=frozenset()` or instrument the
  client with `declared_provider` when billing goes elsewhere.
- **Coexistence.** Other callback handlers (LangSmith-style tracers) and
  OpenTelemetry spans keep receiving their events; `run_inline = True` keeps the
  handler in the caller's context, also under `asyncio`.

## Verified here

`tests/acceptance/phase3_integrations/test_c02_langgraph.py`: the frozen
reference workload reconciles exactly through the graphs for two versions;
re-delivery is a no-op; fan-out, subgraph, interrupt and resume charge each
physical call once; handler-only and handler-plus-provider-wrapper ledgers are
identical; streaming with usage, fallback and error charges; a second callback
handler and OTel spans unaffected; clean installs at both pins.

## Not verified

No live provider call was made. `ChatAnthropic` and other integrations are
covered only through the framework's response metadata contract, not exercised.
LangSmith itself is not installed here; a stand-in handler represents it.
Independent reviewer execution is pending.
