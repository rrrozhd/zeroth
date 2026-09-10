# C04 — CrewAI (Python crews and flows)

Capture every LLM call an existing CrewAI application makes, through CrewAI's
own event bus, without changing crews, tasks, flows or their outputs. Status:
**conformance candidate, not an accepted support row**; see
`tests/acceptance/phase3_integrations/manifest.json`.

## Install

```bash
python -m pip install /path/to/zeroth/packaging/sdk -r requirements-current.txt
```

Tested pins: `requirements-floor.txt` (crewai 1.14.0, the first release whose
completed-call event carries usage) and `requirements-current.txt`. No Zeroth
runtime, database or console is installed.

## The change

`app.py` is the application (sequential crews, a crew with `async_execution`
tasks, a `Flow`, a fallback policy, an application-level retry). It does not
import Zeroth. `check.py` shows the instrumentation:

```python
from zeroth.instrumentation import Recorder
from zeroth.instrumentation.crewai import instrument_crewai
from zeroth.sdk import ZerothClient

listener = instrument_crewai()          # once per process; further calls return it
recorder = Recorder(ZerothClient(api_key=KEY, base_url=URL), raise_on_error=False)

with recorder.run("research-crew", "2026.09", request_id).active() as run:
    result = crew.kickoff(inputs={"topic": topic})
    run.tool_charge("search", tool="web_search", cost_usd=Decimal("0.005"))
    run.summary("completed", metadata=listener.aggregates(run))
    run.outcome(customer_accepted, maturity="final")
```

- **Physical calls.** Each `LLMCallCompletedEvent` is one charge from its usage
  (cached prompt tokens subtracted from input), named after the agent role or
  task (`planner`, `planner#2`); an `LLMCallFailedEvent` is an unmeasured
  attempt with the error text. Agent retries (`max_retry_limit`) therefore show
  as one charge per attempt.
- **Aggregates.** Crews, tasks and agent executions are counted in
  `listener.aggregates(run)` for the run summary; they are never charged.
- **Attribution.** The listener reads the active run where the call is made.
  CrewAI delivers events with the emitter's context, so sequential crews, flows
  and `kickoff_async` attribute directly; tasks with `async_execution=True` run
  on plain threads, so calls are attributed through the crew that kicked off
  inside the run. Two crews in two threads each get their own run.
- **Identity.** Name models in CrewAI's `provider/model` form
  (`openai/gpt-4.1-mini`, `anthropic/claude-haiku-4-5-20251001`); that prefix is
  the declared billing provider. A bare name is recorded with provider `unknown`
  and stays unmeasured unless `instrument_crewai(default_provider="openai")`.
- **Registration.** `instrument_crewai()` registers one listener on the global
  bus; calling it again returns the same listener, so it cannot double count.
- **Streaming.** A streamed call is charged from the same completed event.

## Verified here

`tests/acceptance/phase3_integrations/test_c04_crewai.py`, in clean pinned
environments against the local server: the frozen reference workload reconciles
exactly through crews (v1 at both pins, v2 at current); two concurrent crews on
two runs show no leakage; a retried agent call yields one unmeasured and one
estimated charge; a flow's two calls are charged; `async_execution` tasks land on
their run; a bare model name stays unmeasured; a second `instrument_crewai()`
adds nothing.

## Not verified

No live provider call; streaming and tool-calling LLM modes are not exercised
(fake LLMs answer in ReAct form). CrewAI's own token counters are untouched.
Independent reviewer execution is pending.
