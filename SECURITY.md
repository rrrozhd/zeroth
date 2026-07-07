# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately via
[GitHub Security Advisories](https://github.com/rrrozhd/zeroth-core/security/advisories/new).
Do not open public issues for security reports. You should receive an initial
response within a few days.

## Supported versions

Only the latest released version receives security fixes.

## Deployment hardening notes

Zeroth is a governance-focused runtime; a few defaults matter when deploying it:

- **Never expose the bundled Regulus backend (`src/econ_plane`) standalone.**
  Its token issuer has no credential check of its own. The supported path is the
  in-process mount under `/regulus`, which sits behind Zeroth's API-key gate.
  Zeroth additionally blocks the econ token-issuer endpoint
  (`POST /regulus/**/auth/token`) at the gate, so it is unreachable over HTTP even
  with a valid key; Zeroth's own self-calls mint their econ token in-process.
- **`ECP_JWT_SECRET` is required when Regulus is mounted.** The service fails
  closed at startup if the bundled backend still uses its placeholder secret
  (`change-me`). For local development only, set `ECP_ALLOW_INSECURE_JWT_SECRET=1`
  to bypass the guard.
- **Studio and cost APIs enforce RBAC.** Workflow authoring requires the
  operator/admin tier; reading cost/spend is admin-only (consistent with the
  metrics endpoint). Scope issued API keys to the least role that a caller needs.
- **Budget enforcement fails open by design** (decision D-12): if the econ
  backend is unreachable, runs proceed rather than halt — but the fail-open is now
  logged at WARNING so you can alert on caps not being enforced. Monitor econ-plane
  availability if budget caps are a hard requirement for you.
