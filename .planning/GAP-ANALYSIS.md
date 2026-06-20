# Zeroth — Gap Analysis

**Date:** 2026-06-20
**Scope:** `zeroth-core` @ commit `151cb16` (v0.2.0, milestone v4.1 in progress)
**Method:** Architecture-graph review + targeted subsystem verification (auth, persistence,
guardrails, observability, agent runtime, orchestration). Verdicts are evidence-backed with
file references; "untested hotspot" graph signals were confirmed against the actual test suite
before being reported.

## TL;DR

Zeroth is mature on infrastructure and governance: full RBAC + JWT/OIDC + real multi-tenant
isolation, Postgres **and** SQLite, operational guardrails (rate-limit / quota / dead-letter),
external MCP client support, HITL approvals with pause/resume, audit continuity, econ tracking,
and near-zero TODOs. The genuine gaps cluster in three areas — **agent quality/safety**,
**real-time**, and a handful of **roadmap-acknowledged** persistence items.

## Confirmed strengths (verified — not gaps)

| Area | Status | Evidence |
|------|--------|----------|
| Auth (API keys + JWT/OIDC) & RBAC | FULL | `service/auth.py`, `service/authorization.py`, `identity/models.py` |
| Multi-tenant isolation | FULL | scope checks in `service/authorization.py`, `service/run_api.py` |
| Persistence (Postgres + SQLite, swappable) | FULL | `storage/factory.py`, `storage/async_postgres.py` |
| Operational guardrails (rate-limit/quota/dead-letter) | FULL | `guardrails/rate_limit.py`, `guardrails/dead_letter.py` |
| Tool calling + external MCP servers | FULL | `agent_runtime/tools.py`, `agent_runtime/mcp.py` |
| HITL approvals (pause/resume/edit) | FULL | `approvals/service.py`, `orchestrator/runtime.py` |
| Orchestration primitives (agent/code/approval/subgraph/parallel/conditional/retry/loop) | FULL | `graph/models.py`, `parallel/`, `subgraph/` |
| Orchestrator-core test coverage | COVERED | `tests/orchestrator/`, `tests/parallel/test_drive_integration.py`, `tests/policy/` (139 test files; 39 touch drive/fan-out) |

## Gaps

### Tier 1 — cut against the "governed, production-grade multi-agent" positioning

1. **No agent output-quality evaluation** (ABSENT) → `EVAL` — ✅ ADDRESSED 2026-06-20
   No eval datasets, LLM-as-judge, or scoring existed; `OutputValidator` checks *shape*, not
   correctness. **Fixed:** new `zeroth.core.eval` module — `EvalDataset`/`EvalCase`,
   deterministic scorers + `LLMJudgeScorer`, `run_eval` over a decoupled `target` callable, and
   an `EvalReport` + absolute `gate()` for CI. Errors are first-class (errored ≠ score 0), so a
   flaky judge can't read as a regression. See EVAL-01..05 in `REQUIREMENTS.md`.

2. **No prompt-injection / untrusted-output defense** (weak PARTIAL) → `MBND` — ✅ ADDRESSED 2026-06-20
   Tool results and memory content were re-injected into the model verbatim
   (`agent_runtime/runner.py` `build_tool_message`); the only sanitizer (`audit/sanitizer.py`)
   redacted *audit logs*, not prompts. **Fixed:** `agent_runtime/sanitization.py`
   (`ToolOutputSanitizer` + `HeuristicInjectionScreener`) now length-caps, screens, and
   provenance-wraps untrusted tool/MCP/memory output before it re-enters the model, enabled
   by default via `AgentConfig.tool_output_safety`. See MBND-01..04 in `REQUIREMENTS.md`.

3. **AI-safety guardrails absent** (ABSENT) → `SAFE` — ✅ ADDRESSED 2026-06-20
   `guardrails/` was *operational* only (rate-limit, quota, dead-letter). **Fixed:**
   `guardrails/content.py` (`ContentGuardrail`, `PIIFilter`, `BlocklistFilter`) adds
   PII detection/redaction + blocklist filtering at the agent input/output boundary
   (flag / redact / block), opt-in via `AgentConfig.content_safety`; blocks persist a
   `rejected` audit record. See SAFE-01..03 in `REQUIREMENTS.md`.

### Tier 2 — capability gaps

4. **No streaming** (ABSENT) → `STREAM` (currently out-of-scope; flagged contestable)
   Buffered request/response only (`agent_runtime/provider.py`). Deferred as "async sufficient";
   contestable for any interactive/agentic UX where time-to-first-token matters.

5. **Observability stops at metrics** (PARTIAL) → `OBS` — ✅ ADDRESSED 2026-06-20
   Prometheus metrics + correlation IDs + structured logs, but no distributed tracing.
   **Fixed:** `observability/tracing.py` (`start_span`, `configure_tracing`) adds optional
   OpenTelemetry spans across run → node → agent → tool and fan-out/subgraph hops, with
   trace context propagating across fan-out branches. Off by default; enabled via
   `ZerothSettings.tracing` + the `[otel]` extra. See OBS-01..03 in `REQUIREMENTS.md`.

6. **RAG is plumbing, not a primitive** (PARTIAL) → `RAG` — ✅ ADDRESSED 2026-06-20
   Vector connectors existed for *memory* but there was no retrieval node or ingestion pipeline.
   **Fixed:** `RetrievalNode` (first-class graph node) queries a connector and outputs grounded
   context for a downstream agent; `rag/ingestion.py` (`chunk_text`, `ingest_documents`) chunks
   and writes documents (embedding delegated to the connector). Retrieved chunks are audited with
   source attribution. See RAG-01..03 in `REQUIREMENTS.md`.

7. **Deferred resilience: model-fallback chains + LLM/semantic caching** (ABSENT) → `PRES` — ✅ ADDRESSED 2026-06-20
   A provider/model outage had no automatic failover and there was no response caching.
   **Fixed:** `agent_runtime/resilience.py` adds `FallbackProviderAdapter` (model fallback
   chains, failover on transient errors) and `CachingProviderAdapter` (exact-match response
   cache) — composable `ProviderAdapter` wrappers. Semantic caching remains deferred
   (`ResponseCache` protocol is the extension point). See PRES-01..02 in `REQUIREMENTS.md`.

## Roadmap-acknowledged, in-flight (v4.1)

| ID | Gap | Status |
|----|-----|--------|
| ORCH-03 | reduce/merge/custom merge strategies (only `collect` solid) | Partial — enum + `parallel/reducers.py` scaffolded; roadmap lists in-progress |
| ARTS-01/02/03 | S3/GCS artifact backends (Redis + filesystem only today) | Pending (Phase 45) |
| TREG-01/02/03 | Template registry persistence (in-memory only — lost on restart) | Pending (Phase 46) |
| CBRK-01/02/03 | Durable circuit-breaker state across restarts | Pending (Phase 47) |

## Deliberate trade-off worth naming

**Budget enforcement fails open (D-12).** If Regulus (the cost meter) is unreachable, runs
proceed unmetered. Sensible for availability, but the cost guardrail is best-effort, not hard.

## Recommended priority

1. ~~**EVAL** — agent eval/judge harness.~~ ✅ **Done 2026-06-20.** Highest value; most undercuts "production-grade."
2. ~~**MBND** — tool-output / prompt-injection hardening.~~ ✅ **Done 2026-06-20.** Cheapest way to make "governed" true at the model boundary.
3. ~~**OBS** — OpenTelemetry tracing.~~ ✅ **Done 2026-06-20.** Unblocks debugging multi-hop runs.

---
*Tracked as candidate v4.2 requirement groups in `REQUIREMENTS.md`.*
