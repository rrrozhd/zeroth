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
- **Set `ECP_JWT_SECRET` in production.** The bundled backend ships a
  placeholder JWT secret (`change-me`) intended for local development only.
- **Scope API keys deliberately.** Any valid Zeroth API key can currently reach
  the mounted econ plane's token issuer; treat every issued key as able to read
  economic telemetry until finer-grained RBAC lands on the studio and cost APIs.
- **Budget enforcement fails open by design** (decision D-12): if the econ
  backend is unreachable, runs proceed rather than halt. Monitor econ-plane
  availability if budget caps are a hard requirement for you.
