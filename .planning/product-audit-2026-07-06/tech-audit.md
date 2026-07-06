# Zeroth — Technical Audit (working tree, branch `feat/console-frontend`, 2026-07-06)

Scope: architecture, code quality of load-bearing modules, tests, security posture, production readiness.
Working tree audited as-is (uncommitted econ/service/frontend changes included).
Dedupe source: `.planning/audit-console-backend.md` (2026-07-01) — its findings re-checked against the working tree; status noted per item.

---

## 1. Architecture

### Subsystem map

| Subsystem | Location | Role |
|---|---|---|
| Orchestrator | `src/zeroth/core/orchestrator/runtime.py` (2,232 lines, single class) | Drives graph execution: dispatch, parallel fan-out, policy gates, audit writes, history |
| Graph model | `src/zeroth/core/graph/` (models, validation, versioning, repository, serialization, diff) | Typed node/edge model with discriminated `node_type`, draft→published→archived lifecycle |
| Policy/governance | `src/zeroth/core/policy/` (guard, registry, models) + `audit/` (repository, verifier, models) | Capability allow/deny per node, tamper-evident audit hash chain |
| Econ/cost plane (Zeroth side) | `src/zeroth/core/econ/` (adapter, budget, client, cost, waste, service_auth) | Cost-tracking provider decorator, fail-open budget enforcer, waste analytics |
| Bundled Regulus backend | `src/econ_plane/` (13 sub-apps: auth, instrumentation, dashboard, costing, …) | Standalone FastAPI app, own ECP_ settings/DB/JWT; mountable at `/regulus` |
| Vendored SDK | `src/econ_instrumentation/` | Fire-and-forget telemetry transport (background thread + retry queue) |
| Memory connectors | `src/zeroth/core/memory/` (redis, pgvector, chroma, elastic + registry/factory) | Pluggable KV/thread/vector memory behind a resolver |
| Service/API layer | `src/zeroth/core/service/` (app, bootstrap, auth, authorization, run/audit/approval/cost/webhook/studio APIs) | Deployment-bound FastAPI wrapper; /v1 + unversioned compat routers |
| Storage | `src/zeroth/core/storage/` (async_sqlite, async_postgres, redis, factory) | Protocol-based AsyncDatabase; Alembic migrations under `core/migrations` |
| Frontend console | `frontend/` (Next.js 16, React 19, @xyflow) | Static-export console mounted at `/console` or standalone |

### Boundary assessment

**Clean:**
- Storage behind an `AsyncDatabase` Protocol (`storage/database.py:28`) — SQLite/Postgres swap is real.
- Regulus integration is HTTP-only even when mounted in-process (`app.py:229-246`): Zeroth's econ client/budget/cost call `/regulus/v1` over HTTP with a `headers_provider`, never importing econ_plane internals except at mount/bootstrap points. Deliberate and documented (D-12/D-16).
- `econ/waste.py` is exemplary: pure, in-process, no I/O, well-documented invariants (confirmed vs flagged dollars never overlap).
- Studio API maps canvas nodes 1:1 onto real graph models rather than a parallel schema (`studio_api.py:81-87`).

**Awkward couplings:**
- **`app.py` reaches into econ_plane's guts at three points** — `econ_plane.common.bootstrap` + `init_otel_metrics` in lifespan (`app.py:70-74`), the mount itself (`app.py:238-240`), and `econ/service_auth.py:43` importing `econ_plane.config.settings` to sign tokens with the *backend's* secret. The last one is the real boundary break: Zeroth's "client" side authenticates by sharing the server's private signing key rather than holding a credential. Works only in the bundled topology; silently degrades (fail-open) in separate-process mode.
- **`app.py:225` reuses `auth_config.api_keys[0].secret`** — an externally-issued client credential — as the internal self-auth key. Key #0 gains an implicit second identity; rotating it breaks internal calls.
- **Orchestrator god-module**: `RuntimeOrchestrator` (runtime.py:153) owns dispatch, parallel fan-out (~500 lines, :626-1105), policy enforcement, approval gating, audit chaining, memory injection, webhooks, artifact TTLs. `_drive` alone spans :278-548. No circular imports found, but the class is the coupling point for everything.
- **Dual routing**: every route family is registered twice (versioned + unversioned compat, `app.py:307-342`) — fine for now, but 9 register-calls × 2 is copy-paste debt.
- `bootstrap.py`'s `ServiceBootstrap` dataclass has 15 `object | None`-typed fields (`bootstrap.py:114-153`) — the type system has given up on the wiring container; typos in `getattr(bootstrap, "...")` consumers would fail silently.

No circular imports detected between subsystems.

---

## 2. Severity-ranked findings

### CRITICAL

**C-1. econ_plane `/auth/token` issues Admin JWTs to anyone, no credential check**
`src/econ_plane/auth/api.py:13-16` → `auth/service.py:12-34`: `POST /v1/auth/token` takes `{sub, email, roles}` and returns a signed JWT for **whatever roles the caller asks for** — no password, no secret, nothing. `issue_token` even auto-creates the user row. In standalone topology this is a fully open admin endpoint. In the mounted topology it sits behind Zeroth's API-key gate (`app.py:253-296`), so **any Zeroth API key holder — including a role-less key with zero Zeroth permissions — escalates to econ_plane Admin** (write cost events, change enforcement policies, read all tenants' KPIs). The `require_roles` machinery (`auth/deps.py:19-25`) is theater when the token mint is unauthenticated.

**C-2. econ_plane JWT secret defaults to `"change-me"` (HS256), no startup guard**
`src/econ_plane/config.py:6-7`. Nothing refuses to boot with the default. Anyone who can reach any econ_plane endpoint and knows the default can mint arbitrary Admin tokens offline. Compounded by C-1 (you don't even need the secret) and by `econ/service_auth.py:56` which signs Zeroth's own service tokens with this same secret. Docs (`docs/how-to/deployment/with-regulus.md`) say to set `ECP_JWT_SECRET`, but the code fails unsafe if you don't.

### HIGH

**H-1. Studio API has zero authorization — any authenticated principal can author/archive workflows**
`src/zeroth/core/service/studio_api.py:281-429`: none of the 7 routes calls `require_permission`; there is no `studio:*`/`graph:write` permission in `authorization.py:13-25` at all. Every other route family enforces RBAC (e.g. `run_api.py:102,140`). A role-less API key (valid credential, empty `roles`) can create, structurally edit, and archive (soft-delete) workflows — the governance-critical authoring surface is the one without RBAC. Same for `cost_api.py:48-96` (tenant/deployment spend readable by any authenticated principal; no `Permission.METRICS_READ` check).

**H-2. Audit hash-chain race under parallel fan-out (pre-existing, now confirmed by storage inspection)**
`audit/repository.py:34-45` does read-chain-head → compute digest → insert. `async_sqlite.py:66-80` opens a **fresh connection per transaction** in WAL mode with no lock and no `BEGIN IMMEDIATE`; WAL readers don't block, so two concurrent branch writers (parallel branches write audits from `create_task`-spawned coroutines, `runtime.py:780,818-824`) can read the same head and both insert with the same `previous_record_digest` — forking the tamper-evident chain. The verifier (`audit/verifier.py:60-66`) will then report "previous digest mismatch" on a legitimate run. Dedupe: `.planning/audit-console-backend.md` P1 [likely] — **still unfixed**; the storage-layer evidence upgrades it from likely to structural. Fix: per-run `asyncio.Lock` around `AuditRepository.write`, or `BEGIN IMMEDIATE` + retry.

**H-3. Budget enforcement and cost pipeline fail open, silently**
- `econ/budget.py:78-80`: bare `except Exception: return True, 0.0, inf` — no logging, no metric. An outage, misconfigured URL, or 401 (bad self-auth) permanently disables budget caps with no operator signal. Fail-open is a documented decision (D-12); *silent* fail-open is not — there is not even a `logger.warning` distinguishing "under budget" from "Regulus unreachable for the last 6 hours".
- `econ/service_auth.py:44-58`: both `except Exception: return None` swallow config errors the same way — a typo'd `ECP_JWT_SECRET` shows up as "budget always allows".
- `econ_instrumentation/transport.py:73-76,123-126`: events silently dropped when the buffer overflows or retries exhaust; `dropped_events` is counted but never logged/exported. Cost under-counting is invisible.

**H-4. Cross-tenant reads in econ_plane: tenant is caller-chosen**
`econ_plane/common/tenant.py:4-5`: `resolve_tenant_id(tenant_id) -> tenant_id or default`. Tenant comes from the **query param**, not from JWT claims — the token doesn't even carry a tenant (`auth/service.py:27-33`). Any Viewer-tier token reads any tenant's KPIs/costs (`dashboard/api.py:40+`, all endpoints allow Viewer). On the Zeroth side, `GraphRepository` has no tenant column at all (grep for `tenant` in `graph/repository.py`: zero hits) — combined with H-1, all workflows are one flat namespace across tenants. Zeroth's run/thread/audit repos *do* scope by tenant (`runs/repository.py:460,494,911`; `tests/service/test_tenant_isolation.py` exists) — the gap is specifically graphs + econ.

**H-5. JWKS fetched with `urlopen`, no timeout, no cache, on every bearer verification**
`service/auth.py:114-116`: `_load_jwks` uses blocking `urllib.request.urlopen` (inside the async auth middleware path) with **no timeout** — a hung IdP hangs the event loop worker indefinitely — and is re-fetched per `verify()` call when `jwks` isn't inlined (`auth.py:93`). No caching, no rotation handling, no error differentiation.

### MEDIUM

**M-1. econ_plane runs `create_all` + ~40 idempotent `ALTER TABLE` checks on every request**
`econ_plane/database.py:17-24`: `get_db` (the FastAPI dependency) calls `Base.metadata.create_all` and `_ensure_sqlite_compat()` (dozens of `PRAGMA table_info` + conditional ALTERs, :27-82) **per request**. Severe latency tax and a de-facto admission that econ_plane has no real migration story at runtime (Alembic scaffolding exists at `econ_plane/_migrations/env.py` but the serving path doesn't use it). Also sync SQLAlchemy sessions inside async FastAPI (blocking the loop unless routes are `def` — they are, so threadpool, but the per-request DDL still hits every worker).

**M-2. API keys stored and compared in plaintext**
`service/auth.py:30-41,143-151`: static keys arrive via `ZEROTH_SERVICE_API_KEYS_JSON` env (plaintext secrets in env/process table) and are stored/compared as plaintext (`hmac.compare_digest` is constant-time, good, but a DB/env leak yields usable credentials directly). No hashing, no key prefix/last-4 model, no rotation/expiry metadata. A `secrets/provider.py` module exists but isn't used for service credentials.

**M-3. Zero-duration audit timestamps (pre-existing, still open)**
Dedupe of `.planning` audit "completed_at == started_at": write sites still pass `completed_at=datetime.now(UTC)` computed *before* the record's `started_at` default-factory fires (`runtime.py:791,1713,1775`), so the clamp validator in `audit/models.py` fires on ~every record and every node shows zero duration; `started_at` is construction time, not dispatch time. Real timing survives only in OTEL spans. **Not fixed in working tree.**

**M-4. econ_plane deprecated lifecycle + unauthenticated `/metrics`**
`econ_plane/main.py:60-63` uses deprecated `@app.on_event("startup")` (never fires when mounted — worked around in `app.py:63-77`, so two divergent init paths). `main.py:71-76`: `/metrics` renders per-tenant Prometheus metrics with **no auth** — fine mounted (behind the API-key gate), an information leak in the standalone topology.

**M-5. Broad `except Exception` swallows in the orchestrator hot path**
Improved since the planning audit (`_typed_audit_fields` now logs, `runtime.py:2081-2087,2094-2098` — planning-audit P2 **fixed**), but others remain: `_refresh_artifact_ttls` swallows all errors (`runtime.py:275-276`), branch history rebuild falls back to raw dicts silently (`runtime.py:992-993`), the econ-instrumentation import in dispatch is `except ImportError: pass` (`runtime.py:1250-1251`) — an env with a half-installed extra silently stops billing.

**M-6. `run_id` namespace collision in audit chain for service denials**
`service/auth.py:207`: denial records use synthetic `run_id=f"service:{method}:{path}"`. All denials for the same method+path chain together forever under H-2's read-head-then-insert scheme — concurrent 401s on a popular path race the same chain head, and the per-run chain-verification semantics get stretched over an unbounded, unrelated record set.

### LOW

**L-1. Console/frontend**: API key in `localStorage` (`frontend/app/lib/config.ts:11-27`) — XSS-exfiltratable; acceptable MVP tradeoff, deserves a comment + future httpOnly-proxy plan. `/console` path auth bypass (`app.py:262-267`) serves static assets only — verified safe as long as nothing dynamic is ever mounted under it. Planning-audit P1 on `ui.tsx` tone gaps (**fixed**: `ui.tsx:98-152` now covers `queued`, `waiting_interrupt`, `error`, `forbidden`, `unauthenticated`, `terminated_by_loop_guard`).
**L-2. `verify-*.png` screenshots at repo root** (untracked) — hygiene; move to scratch or `.planning/`.
**L-3. `except Exception` around `jwt.decode`** marked `pragma: no cover` (`auth.py:103`) hides which failure occurred (expiry vs audience vs signature) from logs — all become "invalid bearer token" with no server-side detail.
**L-4. Dead/latent code**: `auth.py:20-23` optional `jwt` import with "graceful until dependency is added" comment, but `PyJWT[crypto]` is a hard dependency in `pyproject.toml` — the None-guard paths (`auth.py:87,119`) are unreachable dead branches.
**L-5. `budget.py:61-62`**: test seam `_transport` wrapped in `httpx.MockTransport` inside production code — works, but a test double leaking into the prod constructor signature.

### Planning-audit dedupe summary
| `.planning/audit-console-backend.md` finding | Status in working tree |
|---|---|
| P1 branch `RunHistoryEntry` not redacted | **FIXED** (`runtime.py:771-772,800-809`) |
| P1 ui.tsx tone gaps | **FIXED** (`ui.tsx:98-152`) |
| P1 audit-chain race in fan-out | **OPEN** — escalated here as H-2 with storage-layer evidence |
| P2 denial records missing `completed_at` | **FIXED** (`auth.py:224-228`) |
| P2 `_typed_audit_fields` silent drops | **FIXED** (logs at `runtime.py:2081-2087,2094-2098`) |
| P2 `_auto_layout` collapse / all-or-nothing positions | **FIXED** (`studio_api.py:174,186-190,213-222`) |
| P2 completed_at==started_at zero duration | **OPEN** — M-3 |

---

## 3. Tests

- **1,440 tests collected** (16 deselected) across ~40 top-level files + 25 subdirectories — broad, disciplined coverage: orchestrator, parallel, policy, guardrails, approvals, webhooks (6 files), memory, rag, sandbox, storage (incl. `test_postgres_backend.py`), service E2E (`tests/service/test_e2e_phase4/5.py`), tenant isolation (`tests/service/test_tenant_isolation.py`), RBAC (`test_rbac_api.py`), regulus mount/bootstrap (`test_regulus_mount.py`, `tests/service/test_regulus_bootstrap.py`).
- **Critical paths lacking tests:**
  1. **Concurrent audit-chain writes** — `tests/audit/test_audit_repository.py` has no concurrency case; H-2 would be caught by a 2-branch gather + chain-verify test (the planning audit prescribed exactly this repro; still absent).
  2. **Studio authorization** — `test_studio_api.py` covers CRUD behavior, not the *absence* of RBAC (H-1); no test asserts a role-less key is rejected.
  3. **econ_plane entirely untested from this repo** — no `tests/econ_plane/`; the bundled backend (13 sub-apps, auth included) ships on vendored trust. C-1 would fall out of the first auth test written.
  4. **Fail-open observability** — tests assert budget fail-open behavior (`test_econ_budget.py`) but nothing asserts an operator-visible signal, because there is none (H-3).
  5. **Frontend: zero test/lint infrastructure** — `frontend/package.json:5-10` has only `dev/build/start/gen:api`; no vitest/playwright/eslint script, and CI never builds the frontend.
  6. Branch-level typed audit fields E2E (planning-audit minor) — still absent.

## 4. Security posture (summary)

- **Zeroth service auth**: solid shape (constant-time key compare, JWT bearer with issuer/audience, RBAC map `authorization.py:28-42`, denials audited) — undermined by H-1 (studio/cost routes outside RBAC), H-5 (JWKS fetch), M-2 (plaintext keys).
- **econ_plane auth**: broken (C-1, C-2, H-4). Treat the bundled backend as *unauthenticated* today and rely entirely on the outer Zeroth gate; do not deploy it standalone-exposed.
- **Tenant isolation**: enforced for runs/threads/audits; absent for graphs and all econ data (H-4).
- **Secrets**: redaction pipeline is genuinely good (`_redact_for_audit` at every audit/history write site, `runtime.py:771-806,1685-1687,1755,1862-1866`; templates redaction `runtime.py:1193-1208`). At-rest gap closed by the branch-history fix. Env-JSON plaintext credentials remain (M-2). Optional SQLite field encryption exists (`async_sqlite.py:59-62`).
- **Fail-open inventory**: budget (by design, silent — H-3), self-auth token mint (silent), econ instrumentation import (`runtime.py:1250-1251`), telemetry drops (silent), `/console` + `/health` auth bypass (by design, safe), CORS only when configured (good, `app.py:347-355`).

## 5. Production readiness

- **Persistence/migrations**: Zeroth core — Alembic wired via `bootstrap.run_migrations` (`bootstrap.py:84-95`), SQLite WAL + Postgres backend, checkpointing, leases. Solid. econ_plane — per-request `create_all` + hand-rolled ALTERs (M-1): not production-grade.
- **Observability**: correlation IDs end-to-end (`app.py:268-296`), OTEL tracing/metrics, queue-depth gauge, Prometheus in econ_plane. Gap: the fail-open paths are exactly the ones with no signal (H-3).
- **Deployment story**: `docs/how-to/deployment/` covers 5 real modes (local, standalone+nginx/systemd, embedded, sandbox container, with-regulus) with an honest mode-comparison table — above-average docs. No first-party Dockerfile/compose for the standalone service; graceful shutdown (SIGTERM → drain → release leases) is implemented (`app.py:122-156`).
- **CI**: single job — ruff + full pytest + interrogate on Python 3.12 (`.github/workflows/ci.yml`), plus docs/examples/release workflows. Missing: frontend build/lint, Postgres-service matrix job, any econ_plane test run, security scanning.

### Verdict
Zeroth core (orchestrator, graph, policy, audit, service layer) is unusually mature for its stage — tested, documented, deliberate about redaction and failure semantics. The bundled Regulus backend is a different codebase in quality terms and is the source of both Critical findings; until C-1/C-2/H-1 land, the platform's governance guarantees hold only against actors who have *no* credential at all.

**Fix order**: C-1 → C-2 → H-1 → H-2 → H-3 (all are days, not weeks).
