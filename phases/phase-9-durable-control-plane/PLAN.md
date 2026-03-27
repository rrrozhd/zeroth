# Phase 9 — Durable Control Plane & Production Operations Plan

## Goal

Replace the in-process MVP execution control path with a durable, observable, production-safe control plane.

## Why This Phase Exists

The current service wrapper creates background tasks inside one FastAPI process and relies on in-memory task tracking during process lifetime. The root progress log already notes that durable job supervision is outside the Phase 4 MVP. To reach the intended production posture, Zeroth needs durable dispatch, recovery, rate control, and operational visibility.

## Scope

- Durable run dispatch and worker supervision
- Restart-safe continuation for approvals, interrupts, and queued work
- Operational guardrails such as quotas, rate limits, and backpressure
- Metrics, tracing, structured logs, and administrative control surfaces

## Out Of Scope

- A full UI control plane
- Multi-region control-plane federation

## Relevant Code Areas

- `src/zeroth/service/app.py`
- `src/zeroth/service/run_api.py`
- `src/zeroth/service/bootstrap.py`
- `src/zeroth/orchestrator/runtime.py`
- `src/zeroth/runs/models.py`
- `src/zeroth/runs/repository.py`
- `src/zeroth/approvals/service.py`
- `src/zeroth/storage/redis.py`
- `tests/service/`
- `tests/runs/`
- `tests/approvals/`
- `tests/integration/`

## Workstreams

### 9A. Durable Dispatch And Worker Supervision

Move run execution out of fragile in-process background task state.

Requirements:

- Introduce a durable queue or run-lease mechanism for pending and running work
- Separate API request handling from worker execution ownership
- Ensure workers can resume or safely fail leased work after restart
- Preserve idempotency across duplicate submissions and worker retries

### 9B. Resume And Recovery Semantics

Make paused and interrupted workflows resilient to restarts and worker turnover.

Requirements:

- Persist enough execution metadata to resume approvals, retries, and thread continuation safely
- Ensure run recovery does not duplicate side effects or corrupt thread state
- Add explicit recovery behavior for dispatch crashes, worker crashes, and partial completion

### 9C. Operational Guardrails

Add platform controls that protect availability and fairness.

Requirements:

- Add rate limiting, quota enforcement, and bounded concurrency per tenant or deployment
- Surface backpressure instead of accepting unbounded work
- Define dead-letter or operator-review flows for repeatedly failing runs

### 9D. Observability And Admin Controls

Make the platform operable under production conditions.

Requirements:

- Add metrics for queue depth, run latency, approval wait time, policy denials, and worker failures
- Add tracing or correlation metadata across API, orchestrator, approvals, and audits
- Add structured logs and administrative controls for run interruption, cancellation, and replay-safe inspection

## Acceptance Criteria

- Run execution survives API-process restarts without losing queued or active work
- Approval and thread continuation remain correct across worker handoff
- Rate limits and quotas are enforced before the platform is overloaded
- Operators can observe the system through metrics, correlation-friendly logs, and administrative controls
- Recovery and operations behavior is covered by restart and failure-path tests
