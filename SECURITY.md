# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately via
[GitHub Security Advisories](https://github.com/rrrozhd/zeroth/security/advisories/new).
Do not open public issues for security reports. You should receive an initial
response within a few days.

## Supported versions

Only the latest released version receives security fixes.

## Deployment hardening notes

Zeroth is a governance-focused runtime; a few defaults matter when deploying it:

- **Repository ingress is absent and fails closed.** This release has no public
  repository installation, checkout, or GitHub App ingress, and project
  execution refuses manifests that require checkout material when no trusted
  materializer is configured. The security release matrix records this as an
  absence proof, not as exercised repository behavior. Adding any such endpoint
  or trusted materializer invalidates the proof and requires new tenant-scoped
  behavioral coverage before promotion.

- **Never expose the bundled Regulus backend (`src/econ_plane`) standalone.**
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
- **Budget enforcement fails open by design** (decision D-12): if the econ
  backend is unreachable, runs proceed rather than halt — but the fail-open is now
  logged at WARNING so you can alert on caps not being enforced. Monitor econ-plane
  availability if budget caps are a hard requirement for you.
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
- **MCP servers require capabilities before any process is spawned.** Under
  active enforcement an agent must hold BOTH `process_spawn` and
  `external_api_call` before its configured MCP server subprocesses are started
  — a missing grant denies at startup, not after the side effect. Graph
  validation reports the same requirement at publish time
  (`missing_mcp_capability`), so enforced deployments never learn about the gap
  at dispatch.
