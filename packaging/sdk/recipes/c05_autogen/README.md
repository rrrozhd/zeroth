# C05 — AutoGen AgentChat (Python, 0.7.x)

Capture every model call an existing AutoGen AgentChat application makes by
wrapping each agent's model client, without changing agents, teams, termination
or outputs. Status: **conformance candidate, not an accepted support row**; see
`tests/acceptance/phase3_integrations/manifest.json`.

## Install

```bash
python -m pip install /path/to/zeroth/packaging/sdk -r requirements-current.txt
```

Tested pins: `requirements-floor.txt` (autogen-agentchat 0.7.0) and
`requirements-current.txt` (0.7.5), the selected maintained line. No Zeroth
runtime, database or console is installed.

**AutoGen 0.2 / AG2** (`autogen`, `pyautogen`) is a separate, unsupported row:
importing `zeroth.instrumentation.autogen` in such an environment raises a
diagnostic that says so. Use the explicit SDK events (C07) there.

## The change

`app.py` is the application (an `AssistantAgent`, a `RoundRobinGroupChat`
with `TERMINATE` and message-count termination, a streamed run, a fallback
policy, an application-level retry, an `asyncio.gather` fan-out). It does not
import Zeroth. `check.py` shows the instrumentation:

```python
from zeroth.instrumentation import Recorder
from zeroth.instrumentation.autogen import ZerothChatCompletionClient
from zeroth.sdk import ZerothClient

planner = AssistantAgent("planner", model_client=ZerothChatCompletionClient(
    OpenAIChatCompletionClient(model="gpt-4.1-mini"),
    model="gpt-4.1-mini", provider="openai", step="planner"))

recorder = Recorder(ZerothClient(api_key=KEY, base_url=URL), raise_on_error=False)
with recorder.run("support-team", "2026.09", request_id).active() as run:
    result = await team.run(task=question)
    run.summary("completed", metadata={"stop_reason": result.stop_reason})
    run.outcome(customer_accepted, maturity="final")
```

- **Physical calls.** Each `create` / `create_stream` is one charge from
  `CreateResult.usage` (prompt and completion tokens; AgentChat reports no
  cache split), named by `step` (`planner`, `planner#2`). Multi-agent turns
  are therefore one charge per turn per agent.
- **Identity is declared.** `model` and `provider` are what the wrapped client
  is configured to bill; nothing is inferred from responses. Providers other
  than `openai` and `anthropic`, and unlisted models, stay unmeasured.
- **Failures.** A call that raises, is cancelled through its
  `CancellationToken`, or a stream closed before its final result is an
  unmeasured attempt; the exception propagates unchanged.
- **Missing usage.** A result with zero prompt and completion tokens is an
  unmeasured charge, never zero.
- **Cache hits.** `CreateResult.cached` (AutoGen's `ChatCompletionCache`)
  is not a provider call and records nothing.
- **Termination and outcomes.** Termination is the team's; the outcome is yours.

## Verified here

`tests/acceptance/phase3_integrations/test_c05_autogen.py`, in clean pinned
environments against the local server: the frozen reference workload
reconciles exactly through the agents (v1 at both pins, v2 at current); a
two-agent team stopped by `TERMINATE` charges one call per turn with an
explicit outcome; a streamed run is charged once with its usage; missing usage
is unmeasured; a cache hit records nothing; a cancelled call is an unmeasured
attempt. In this environment: the AG2 diagnostic.

## Not verified

No live provider call. Tool use, handoffs and `OpenAIChatCompletionClient`
itself are not exercised (fake clients stand in). Microsoft Agent Framework
(C09) is a separate row. Independent reviewer execution is pending.
