# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately via
[GitHub Security Advisories](https://github.com/rrrozhd/zeroth/security/advisories/new).
Do not open public issues for security reports. You should receive an initial
response within a few days.

## Supported versions

Only the latest released version receives security fixes.

## Deployment hardening notes

Zeroth is a governance-focused runtime. These are security boundaries, not
production-readiness claims:

- **Browser keys are exchange-only.** The console sends an API key once to
  `POST /v1/auth/session`, then uses a short-lived `Secure`, `HttpOnly`,
  `SameSite=None` cookie. It never stores the key in `localStorage`. Cookie-
  authenticated mutations require an exact same-origin or configured standalone
  console `Origin`. `ZEROTH_CONSOLE_CORS_ORIGINS` accepts exact HTTP(S) origins
  only; wildcards, URL credentials, paths, queries, and fragments are rejected.
  API responses restrict `connect-src` to `'self'`. A standalone console host
  must set an equally restrictive document CSP that names only the API origin.
  Production and secure local operation require a shared
  `ZEROTH_AUTH__BROWSER_SESSION_SECRET` of at least 32 bytes. Ephemeral signing
  exists only behind
  `ZEROTH_AUTH__ALLOW_EPHEMERAL_BROWSER_SESSION_SECRET_DEVELOPMENT=true`, which
  production rejects; it invalidates sessions at restart and must not be used
  with multiple workers.

- **Repository ingress is enabled only when GitHub App configuration is
  enabled.** Claiming an installation requires `repository:admin`; the operator
  role cannot claim it. Installation and repository records, checkout and run
  state, and API lookup are tenant/workspace scoped. Git refs and trees are
  validated before materialization, checkout destinations are contained under
  controlled roots, webhook bodies require GitHub HMAC authentication and
  durable replay handling, and installation tokens are redacted from logs,
  errors, audit payloads, persisted state, and workload environments. Enabling
  this surface requires the repository/GitHub hostile-input, isolation,
  authentication/replay, containment, redaction, and recovery matrices; an
  absence-only test is not evidence for the enabled surface.

- **Untrusted inline and repository units cannot use the local subprocess
  backend by default.** Select Docker or sidecar isolation. The explicit
  `ZEROTH_SANDBOX__ALLOW_UNTRUSTED_LOCAL_DEVELOPMENT=true` compatibility flag is
  for local test/development fixtures only and is rejected in production.

- **Never expose the bundled Regulus backend (`src/zeroth/econ/plane`) standalone.**
  Its token issuer has no credential check of its own. The supported path is the
  in-process mount under `/regulus`, which sits behind Zeroth's API-key gate.
  Zeroth additionally blocks the econ token-issuer endpoint
  (`POST /regulus/**/auth/token`) at the gate, so it is unreachable over HTTP even
  with a valid key; Zeroth's own self-calls mint their econ token in-process.
- **`ECP_JWT_SECRET` — auto-generated ephemeral secret when unset.** The bundled
  control plane is mounted by default, and it signs its Admin tokens with
  `ECP_JWT_SECRET`. Rather than boot on the forgeable placeholder (`change-me`),
  a fresh deploy that leaves it unset **auto-generates a cryptographically-strong
  ephemeral per-process secret** at startup (logged at `WARNING`) — stronger than
  the placeholder, so tokens stay unforgeable with zero configuration. This is
  safe because the only client of `/regulus` is Zeroth's own in-process self-auth
  in the same process (the open token issuer is blocked at the gate), so a
  cross-worker secret mismatch is not a reachable path. **For multi-worker or
  persistent deployments, set an explicit `ECP_JWT_SECRET`** so every worker signs
  and verifies with the same key across restarts. Setting a real `ECP_JWT_SECRET`
  uses it unchanged; `ECP_ALLOW_INSECURE_JWT_SECRET=1` keeps the literal
  `change-me` placeholder (tests / deliberately-insecure local dev only).
- **Studio and cost APIs enforce RBAC.** Workflow authoring requires the
  operator/admin tier; reading cost/spend is admin-only (consistent with the
  metrics endpoint). Scope issued API keys to the least role that a caller needs.
- **Budget enforcement fails closed by default.** An unavailable, malformed, or
  incomplete authoritative budget response denies admission. Explicit
  `fail_closed=false` remains an availability-over-governance compatibility
  choice; do not use it where a cap is a hard requirement.
- **Audit-chain integrity is database-coordinated across workers.** Appends to
  the per-tenant audit hash chain serialize through a database coordination row
  (advisory locking on Postgres, reserved-row locking on SQLite), so multiple
  workers cannot fork the chain by racing on the same head. Chain verification
  detects and reports mixed/legacy segments rather than silently passing.
- **Vault secret resolution is async, pooled, and single-flight.** Cache misses
  resolve through one shared `httpx.AsyncClient` with per-key and AppRole-login
  single-flight locks, so a slow Vault cannot block the event loop and N
  concurrent misses collapse into one fetch. Sync resolution remains only for
  synchronous callers; service paths use the async helpers.
- **Registered MCP processes use an operator-owned Docker isolation profile.**
  Set `ZEROTH_SANDBOX__MCP_ISOLATION_IMAGE` to an immutable image digest. The
  adapter runs without host mounts, as a numeric non-root user, read-only, with
  all capabilities dropped, `no-new-privileges`, bounded CPU/memory/PIDs, a
  no-exec temporary directory, an exact environment-key allowlist, and Docker
  network `none` by default. Registration chooses only the command inside that
  pinned image and its arguments; it cannot choose the image, Docker endpoint,
  mounts, user, limits, or network. Without a configured isolation image,
  discovery and dispatch refuse before spawn with `MCPIsolationRequiredError`.
  The explicit
  `ZEROTH_SANDBOX__ALLOW_UNISOLATED_MCP_DEVELOPMENT=true` flag restores legacy
  host execution for local development only and is rejected in production.
- **An `mcp_tool` node cannot exceed the operator's grants for its server.**
  This is the control that matters, and it is not the bullet above. Operators
  register servers in a table graph authors cannot edit; the node's declared
  `capability_bindings` are checked against that row's `grants` at publish and
  again in `MCPSessionPool` before a process exists — unconditionally, including
  on deployments running without policy enforcement, because the grants are the
  operator's assertion about their own server and do not depend on a policy
  switch. A published version is immutable, so the run-time check is what makes
  narrowing `grants` actually withdraw a capability.
  In isolated mode, `grants` still do not define OS/network policy; the
  operator-owned isolation profile does. If external access is required, use a
  dedicated Docker network plus host/firewall egress rules: a Docker network
  name alone is not a destination allowlist. In development-only legacy mode,
  `command`, `args`, and `env` are passed to the host stdio transport, which is
  arbitrary code execution as the service user.
- **The deprecated inline `agent.mcp_servers` path has no operator side at
  all.** Validation and runtime reject it by default. The explicit development
  flag above permits legacy graphs only for migration/testing; their binary,
  argv, environment, and discovered tools remain author-controlled. Migrate to
  registry-backed `mcp_tool` nodes with `zeroth-core mcp-import`.

- **Gateway-only governance cannot enforce internal tool calls.** Gateway
  admission can authenticate, scope, budget, and audit the outer request, but a
  remote graph may call tools internally without crossing that boundary. Setting
  `require_full_tool_enforcement=true` therefore fails configuration validation.
  Deploy the in-process SDK middleware/tool wrappers when full tool enforcement
  is required.
