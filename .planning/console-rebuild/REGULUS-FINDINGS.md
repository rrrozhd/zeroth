# Regulus / Econ-Plane Console Access — Findings

Date: 2026-07-17 · during P4

## The security boundary (intentional)

- Regulus (`src/zeroth/econ_plane`) protects its data endpoints with its **own**
  JWT (HS256, `ECP_JWT_SECRET`), via `HTTPBearer` + `require_roles(...)`
  (`econ_plane/auth/deps.py`). Roles: **Admin / Analyst / Approver / Viewer**.
- When mounted in-process at `/regulus`, it *also* sits behind Zeroth's API-key
  gate. Zeroth's own self-calls (cost API, budget, ingest) carry **both** an
  `X-API-Key` and a freshly-minted econ **Admin** Bearer
  (`core/econ/service_auth.py::mint_econ_service_token` → `roles:["Admin"]`,
  300s TTL, minted **in-process only**).
- The econ token **issuer** `/regulus/**/auth/token` is **blocked over HTTP**
  (returns 404) in `core/service/app.py` — by design, because it "mints an Admin
  JWT for any caller … with no credential check," which "would let any
  authenticated Zeroth principal escalate to econ Admin (and read cross-tenant
  KPIs)." Cited to SECURITY.md.

**Consequence:** the console (operator with only the Zeroth `X-API-Key`) gets
**401** on every `/regulus/v1/*` data endpoint. It cannot — and by design must
not — obtain an econ token over HTTP.

## The data model is global, not tenant-scoped

- **None** of the 14 `dashboard/*` endpoints take a `tenant_id` param
  (`econ_plane/dashboard/api.py`): KPIs, top-creators, capital-destroyers,
  capability-ranking, trends, drift, calibration — all **global, capability-
  centric aggregates**, role-guarded only.
- econ tokens carry **no tenant claim** (`sub/email/roles/iss/exp` only).
- The only genuinely tenant-scoped econ read is `/budget/status?tenant_id=…`
  (already consumed by the Zeroth cost API; surfaced in the console **Cost**
  screen). Capabilities registry / enforcement / costing are also global.

**Consequence:** a *tenant-scoped* console proxy is **not implementable** for the
dashboards — there is no tenant dimension to pin. Scoping "to the caller's
tenant" only covers budget status (already covered).

## Chosen realization: platform-admin-gated read-only proxy

Since tenant-scoping is impossible, the safe way to bring Regulus data into the
console is a **new main-app router** `/v1/econ/regulus/*` that:

1. Authenticates the request via Zeroth's normal gate **and requires the Zeroth
   `admin` role** (`ServiceRole.ADMIN`) — a platform-operator surface, not a
   per-tenant one.
2. Mints the econ Admin token **in-process** (`make_self_auth_headers_provider`,
   already on `app.state.regulus_self_auth_headers`) — the HTTP issuer stays
   blocked/hidden.
3. Forwards **only read-only GETs** to a fixed allowlist of `/regulus/v1/*`
   read endpoints (dashboard, registry, enforcement list, costing, reconciliation
   summaries). No writes (no enforcement approve/reject, no registry mutations)
   in this pass.
4. Returns the upstream JSON verbatim; on a disabled/absent mount → clean 503/404
   the console renders as "Regulus not enabled".

This mirrors the platform's intent: the block was on *self-escalation by any
principal*; a **read-only, admin-role-gated, in-process-minted** path is the
controlled exception. In single-tenant / self-hosted consoles (the common case)
the admin operator *is* the platform, so "global" = "all of my data".

Writes to Regulus (enforcement approve/reject, capability/registry mutations)
remain out of the console for now — they're higher-risk and better designed
separately.

## Frontend impact

- Regulus nav group shows only when the mount is detected AND the operator has
  admin (else hidden / "not enabled").
- Screens (Econ Dashboard, Capabilities, Enforcement [read], Costing,
  Reconciliation) fetch via `/v1/econ/regulus/*`, not `/regulus/*` directly.
