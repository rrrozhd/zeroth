# C01 — direct OpenAI and Anthropic Python SDK applications

Capture every physical model call of an existing application that uses the
`openai` and `anthropic` Python SDKs directly, without changing what the
application returns, raises or cancels. Status: **conformance candidate, not an
accepted support row**; see `tests/acceptance/phase3_integrations/manifest.json`.

## Install

The capture modules ship in the `zeroth-sdk` source tree (`packaging/sdk`); the
published `0.1.0a1` prerelease predates them, so install from the repository
until the next SDK release:

```bash
python -m pip install /path/to/zeroth/packaging/sdk -r requirements-current.txt
```

Tested pins: `requirements-floor.txt` (oldest supported) and
`requirements-current.txt`. Nothing else is installed: no Zeroth runtime,
database or console. `anthropic >= 1.0` and `openai >= 3.0` are built on
`httpx2`; the SDK itself uses `httpx`. Both coexist.

## The change

`app.py` is the application; it does not import Zeroth. `check.py` shows the
whole change: instrument each client once, open a run per request, label the
steps you care about, charge tools you price yourself, and record the outcome
you own.

```python
from zeroth.instrumentation import Recorder
from zeroth.instrumentation.anthropic import instrument_anthropic
from zeroth.instrumentation.openai import instrument_openai
from zeroth.sdk import ZerothClient

recorder = Recorder(ZerothClient(api_key=KEY, base_url=URL), raise_on_error=False)
instrument_openai(openai_client)          # OpenAI() or AsyncOpenAI()
instrument_anthropic(anthropic_client)    # Anthropic() or AsyncAnthropic()

with recorder.run("support-bot", "2026.09", request_id).active() as run:
    with run.label("plan"):
        plan = assistant.plan(question)
    run.tool_charge("search", tool="web_search", cost_usd=Decimal("0.005"))
    with run.label("answer"):
        answer = assistant.answer(question)
    run.summary("completed")
    run.outcome(customer_accepted, maturity="final")
```

- **Identity.** `workflow` and `workflow_version` are yours; the version that
  changes economics is the one you compare. `run_id` is your request id. Each
  captured call is one `step` (the label, or `chat` / `responses` / `messages`),
  unique within the run (`answer`, `answer#2`). Events and charges are keyed
  `workflow:version:run:step:attempt`, so re-sending is safe.
- **Attempts.** SDK-internal retries (`max_retries`) are physical requests
  without usable usage: each is a charged, unmeasured attempt before the final
  measured one. An application-level retry is a new step occurrence. A call that
  raises is one charged, unmeasured attempt; retries the SDK made before the
  final failure are not individually visible and are marked `retries_unobserved`.
- **Streaming.** Chat completions need `stream_options={"include_usage": True}`;
  responses and Anthropic streams report usage on their own. Usage is final when
  the stream completes. A stream closed, abandoned or broken first is a charged,
  unmeasured attempt (`error="aborted"`).
- **Pricing.** Estimated from the SDK's pinned rate card for the listed models
  (`metadata.rate_card_version`); usage split retained under `metadata.usage`
  with cached tokens subtracted from input. Unlisted models, missing usage and
  proxied endpoints stay unmeasured, never zero.
- **Proxies.** A client whose `base_url` is not `https://api.openai.com` or
  `https://api.anthropic.com` is recorded with provider `unknown` and unmeasured
  unless you pass `declared_provider="openai"` (you are asserting who bills you).
- **Outcomes.** `run.outcome` is your truth; `provisional` may precede `final`.
- **Threads.** The active run is a `contextvars` context; submit thread-pool
  work with `copy_context().run` (see `Assistant.fanout`). `asyncio` tasks
  inherit it.
- **Delivery.** Synchronous per event. With `raise_on_error=False` a failed
  delivery is kept in `recorder.lost` and never reaches your code path.
- **Coexistence.** Your `httpx` event hooks, OpenTelemetry spans and other
  wrappers keep working; the adapter only routes the same call through the SDK's
  raw-response path.

## Verified here

`tests/acceptance/phase3_integrations/test_c01_direct.py`: the frozen reference
workload reconciles exactly through this application for two versions;
re-delivery is a no-op; stream completion and abort; SDK retries and provider
failures; cached tokens, cache writes and tool cycles; async clients including
cancellation; identical outputs and exceptions with and without instrumentation;
proxy identity; hook and span coexistence; clean installs at both pins.

## Not verified

No live provider call was made; real request ids, provider invoices and
rate-card drift are unverified. Images, audio, realtime, batch, files and hosted
tool charges are not captured. Azure OpenAI and OpenAI-compatible proxies have
no billing identity here. Independent reviewer execution is pending.
