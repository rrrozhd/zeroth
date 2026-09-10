# C06 — TypeScript / Node applications using the Vercel AI SDK

Capture every physical model call of an existing Vercel AI SDK application
through the SDK's own step callbacks, with no Zeroth package and no Python.
Status: **conformance candidate, not an accepted support row**; see
`tests/acceptance/phase3_integrations/manifest.json`.

## Install

The application's own dependencies (`package.json`, pinned by `package-lock.json`):
`ai 7.0.97`, `@ai-sdk/openai 4.0.65`, `zod 4.6.1`. `capture.ts` is a file you copy
into your project; it imports nothing. Run with Node ≥ 23.6 (`node check.ts`)
or Node 22.6–23.5 (`node --experimental-strip-types check.ts`); no bundler.

## The change

`app.ts` is the application (`generateText`, a multi-step tool loop with
`stopWhen`, `streamText`, a JSON retry, a `Promise.all` fan-out, abort signals).
It does not import Zeroth; it accepts callbacks to spread into SDK calls and an
accounting hook for its own tool. `check.ts` shows the instrumentation:

```ts
import { Capture } from "./capture.ts";

const capture = new Capture({ url, key, workflow: "support-chat", version: "2026.09", runId });
const result = await capture.call("answer", MODEL, () =>
  generateText({ model, prompt, tools, stopWhen: stepCountIs(3), ...capture.callbacks("answer", MODEL) }));
capture.tool("search", "web_search", "0.00500000");           // a cost you assert yourself
capture.summary("completed", { total_usage: result.totalUsage });
capture.outcome(customerAccepted, "final");
await capture.flush();                                       // before a serverless handler returns
```

- **Physical calls.** `onStepFinish` fires once per model call, including each
  step of a tool loop: each is one charge from `step.usage` (input excludes
  cached tokens; cache reads, cache writes and reasoning tokens retained),
  named `answer`, `answer#2`, ... with the server-reported model id.
- **Aggregates.** `result.totalUsage` for a multi-step generation goes on the
  run summary, which never carries money.
- **Streams.** `streamText` charges when its finish arrives with usage;
  `onAbort` (a consumer stopped reading, or an abort signal) charges the
  in-flight call as an unmeasured attempt with `error="aborted"`.
- **Failures and timeouts.** `capture.call(step, model, fn)` charges a rejected
  call (provider error, `AbortSignal.timeout`) as an unmeasured attempt when no
  step was charged; the rejection propagates unchanged.
- **Identity.** `model` is what you configured; the step's `response.modelId`
  is what the provider reported. Unlisted models stay unmeasured, never zero.
- **Delivery.** Ordered, `fetch` with `AbortSignal.timeout`, idempotent retry
  on 5xx, network errors and timeouts (4xx reported as lost); `flush()` resolves
  when every delivery has been acknowledged or given up, which is what a
  serverless handler must `await` (or pass to `waitUntil`) before returning.
- **Telemetry.** The SDK's `experimental_telemetry` is not used: the capture
  binds to the stable step and finish results.

## Verified here

`tests/acceptance/phase3_integrations/test_c06_vercel.py`, from `npm ci` on the
committed lock against the local server: the frozen reference workload
reconciles exactly for two versions; a tool loop yields two step charges, one
tool charge and a money-free summary carrying the total; a completed stream is
charged with its usage and an aborted stream is unmeasured; a timed-out call is
an unmeasured attempt; an unlisted model is unmeasured; every delivery is
flushed before exit; retry through injected 503, dropped and timed-out requests
reaches exactly-once delivery.

## Not verified

No live provider or hosted deployment; `@ai-sdk/openai` itself is installed but
not exercised (mock models stand in). No Node 22 binary on this machine. The
rate card in `capture.ts` is a copy of the SDK's four rows. Independent
reviewer execution is pending.
