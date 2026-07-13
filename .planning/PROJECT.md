# Zeroth

## What This Is

Zeroth is a governed medium-code platform for building, running, and deploying production-grade multi-agent systems as standalone API services. The platform provides graph-based orchestration, typed contracts, sandboxed execution, human approvals, per-node audit trails with signed provenance, identity/RBAC, tenant isolation, retention/right-to-erasure, deployment provenance, durable dispatch, real LLM provider integration (100+ models via LiteLLM), an embedded economic control plane (cost attribution, budget caps, unit economics), external memory backends, and containerized deployment with health probes.

## Core Value

Teams can author and operate governed multi-agent workflows without sacrificing production controls, auditability, or deployment rigor.

## Current State (package v0.9.x)

**Package:** `zeroth-core` on PyPI, version `0.9.x` — single repo containing the Python backend (`src/zeroth/core`), the embedded economic control plane (`src/zeroth/econ_plane`, mounted in-process at `/regulus` when the `regulus` extra is installed), and the Next.js console (`frontend/`, shipped pre-built via the `console` extra and served at `/console`).

**Tech stack:** Python / FastAPI / Pydantic / SQLAlchemy / Alembic / LiteLLM / ARQ / Docker; frontend: Next.js 16 / React / TypeScript / React Flow.

**Versioning note:** the planning roadmap historically counted milestones as `v1.x`–`v4.x` (see the phase archives under `.planning/`); those roadmap numbers are NOT the package version. The published package follows the integer-chain scheme in `CLAUDE.md` and is currently in the `0.x` series — roadmap "v4.1 Platform Hardening" work shipped inside package versions `0.4`–`0.7`.

**Hardening status (v0.9.x):** the governed+secure parity pass (tenant isolation, capability enforcement, signed provenance, retention, secret provider) shipped in `0.9`; the follow-up hardening pass closed the remaining audit findings — per-dispatch runner isolation, tenant-scoped deployments with workspace ownership, DB-coordinated audit chains across workers, retention TTL correctness with race-free legal holds, MCP capability gating before process spawn, and non-blocking pooled Vault secret resolution.

## Prior Milestones (roadmap numbering)

- **v4.1 Platform Hardening** — parallel subgraph fan-out, reduce merge strategies, artifact storage backends, persistent template registry, durable circuit breaker state.
- **v3.0 Core Library Extraction & Documentation** — `zeroth-core` pip package, docs, Studio split. The separate Vue-based `zeroth-studio` repo was later replaced by the in-repo Next.js console; the Vue stack is fully retired.
- **v2.0 Zeroth Studio** — first visual authoring UI (Vue 3 + Vue Flow, now superseded).
- **v1.x Production Readiness** — orchestration, approvals, identity, dispatch, deployment foundations.

## Requirements

### Validated

- ✓ Governed workflow graph modeling, validation, and versioning
- ✓ Runtime orchestration, approvals, memory, and deployment-bound service APIs
- ✓ Identity, governance evidence, runtime hardening, durable control-plane foundations
- ✓ Real LLM provider adapters via LiteLLM with retry and token audit
- ✓ Embedded economics: cost events, attribution, budget enforcement, unit economics, model right-sizing
- ✓ External memory connectors (Redis KV/thread, pgvector, ChromaDB, Elasticsearch)
- ✓ Durable webhooks, approval SLA timeouts, distributed dispatch, containerized deployment
- ✓ In-repo Next.js console: dashboard, runs, approvals, audit, cost, Studio canvas with publish/deploy
- ✓ Governed+secure parity: tenant isolation, capability enforcement, signed provenance, retention/erasure, secret provider (v0.9)
- ✓ v0.9 hardening: runner isolation, tenant-scoped deployments, cross-worker audit coordination, retention correctness, MCP/Vault hardening

### Out of Scope

- Mobile apps — web-based platform only
- Judge/evaluation subsystem — preserved as extension point
- Real-time streaming — async invocation model sufficient
- Custom LLM hosting — integrates with hosted providers only

## Key Decisions (living)

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Regulus ABSORBED into the zeroth namespace (v0.8) | Single owner, no re-sync; `/regulus` mount + `ECP_` prefix kept for compatibility | ✓ Good |
| In-repo Next.js console replaces separate Vue `zeroth-studio` | One repo, one release cadence; console ships pre-built via extra | ✓ Good |
| LiteLLM as provider abstraction layer | Routes to 100+ models without per-provider adapters | ✓ Good |
| Postgres production storage, SQLite for dev/test | Production needs vs developer experience | ✓ Good — dual backend |
| Budget check fails open by default (D-12) | Control-plane outage must not block runs; logged at WARNING, opt-in fail-closed | ✓ Good |
| PEP 420 namespace `zeroth.core.*` | Room for sibling packages (`zeroth.econ_plane`) without import collisions | ✓ Good |

## Constraints

- **Tech stack**: Python/FastAPI/Pydantic backend — all new work integrates with the existing foundation
- **Backward compatibility**: existing tests must continue passing through all changes
- **Architecture**: modular monolith — new capabilities are new modules, not separate services

## Evolution

This document evolves at phase transitions and milestone boundaries: requirements move between Active/Validated/Out-of-Scope, decisions get logged, and "What This Is" is re-checked for drift.

---
*Last updated: 2026-07-13 — v0.9 hardening pass complete; PROJECT.md refreshed from stale v4.1/Vue-era language to the current package/console architecture.*
