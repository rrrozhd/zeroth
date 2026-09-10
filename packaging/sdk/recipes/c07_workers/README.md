# C07 — explicit economic events from Python or TypeScript workers

Custom workers that emit the sold economic events themselves, with no framework
adapter: a Python worker on the `zeroth-sdk` client and a standalone TypeScript
worker on Node built-ins over the same HTTP contract. Status: **conformance
candidate, not an accepted support row**; see
`tests/acceptance/phase3_integrations/manifest.json`.

## Install

Python: `python -m pip install /path/to/zeroth/packaging/sdk` (httpx and pydantic only).

TypeScript: nothing to install. `worker.ts` imports only `node:util` and
`node:fs/promises`; run it with `node worker.ts` on Node ≥ 23.6, or
`node --experimental-strip-types worker.ts` on Node 22.6–23.5. No npm package,
no bundler, no Python.

## The contract, by hand

Both workers build the same events (`INTERFACE.md`):

- `event_id = workflow:version:run:step:attempt`, `charge_id = "charge:" + event_id`;
  one `cost_role="charge"` per physical call, one money-free `cost_role="summary"`
  per run, outcomes with `maturity` you declare.
- `cost_usd` as a decimal string with eight places, `cost_measurement="estimated"`
  from the rate card (the TypeScript worker embeds a copy of the four tested
  models' rates and does exact integer arithmetic in 1e-8 USD units; the harness
  keeps it in parity with `zeroth.instrumentation.rate_card`), `"measured"` for
  costs you assert yourself (tools), `"unmeasured"` with `cost_usd=null` for
  missing usage or unlisted models. Never zero.
- `metadata.usage` split, `metadata.pricing`, `metadata.charge_kind` as in Python.

## Retry, flush, recovery

Identities are stable, so a delivery whose acknowledgement was lost is safe to
send again: the server answers `"duplicate"` and stores nothing new.

- TypeScript: `fetch` with `AbortSignal.timeout`, retries on 5xx, network errors
  and timeouts with backoff, never on 4xx (reported as lost with the status);
  every delivery is awaited before the process exits, so a serverless handler
  that `await`s the worker flushes by construction.
- Python: `Recorder(client, raise_on_error=False)` keeps failed deliveries in
  `recorder.lost`; the worker replays them a bounded number of rounds before exit.

## Version comparison

`worker.ts --compare v1 v2` creates the outcome definitions and posts
`/v1/decisions/compare`; the Python client does the same through
`compare_versions`. The harness asserts both decisions are identical apart from
the decision id, evaluation time, workflow name and retained calculation inputs.

## Verified here

`tests/acceptance/phase3_integrations/test_c07_workers.py`: both workers
reproduce the frozen reference workload for two versions with identical ledgers
and identical normalised decisions; the TypeScript worker retries 503, dropped
and timed-out requests through the fault proxy to exactly-once delivery, reports
402/409/422 as lost without retrying, reconciles through the
`--experimental-strip-types` path, and imports nothing beyond Node built-ins.

## Not verified

No live provider or hosted deployment. An actual Node 22 binary was not
available on this machine: the floor is exercised through the flag path on the
installed Node only. Independent reviewer execution is pending.
