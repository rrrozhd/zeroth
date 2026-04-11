# Usage Guide: Identity

## Overview

This guide shows how to configure the Zeroth service's authentication layer and how authenticated principals flow through the rest of the runtime. The data shapes come from `zeroth.core.identity`; the verification and HTTP plumbing come from `zeroth.core.service.auth`.

See [Concept: identity](../concepts/identity.md) for the model and [Concept: service](../concepts/service.md) for how routes enforce roles.

## Minimal example

```python
from zeroth.core.identity import ServiceRole
from zeroth.core.service.auth import (
    BearerTokenConfig,
    ServiceAuthConfig,
    StaticApiKeyCredential,
)
from zeroth.core.service.bootstrap import bootstrap_service

# 1. Configure one static API key and (optionally) a JWT bearer verifier.
auth_config = ServiceAuthConfig(
    api_keys=[
        StaticApiKeyCredential(
            credential_id="ops-1",
            secret="demo-operator-key",        # replace with a real secret
            subject="ops@example.com",
            roles=[ServiceRole.OPERATOR, ServiceRole.REVIEWER],
            tenant_id="acme",
            workspace_id="prod",
        )
    ],
    bearer=BearerTokenConfig(
        issuer="https://auth.example.com/",
        audience="zeroth-core",
        jwks_url="https://auth.example.com/.well-known/jwks.json",
    ),
)

# 2. Hand the config to bootstrap_service; routes now enforce it.
service = await bootstrap_service(
    database,
    deployment_ref=deployment.deployment_ref,
    auth_config=auth_config,
    executable_unit_runner=runner,
)

# 3. Configuration can also be loaded from the environment.
from_env = ServiceAuthConfig.from_env()   # reads ZEROTH_SERVICE_API_KEYS_JSON + bearer
```

Once the service is bootstrapped, every request must send either `X-API-Key: demo-operator-key` or `Authorization: Bearer <jwt>`. The verifier returns an `AuthenticatedPrincipal`; `.to_actor()` downgrades it to the `ActorIdentity` that gets stamped onto every [run](../concepts/runs.md), [approval](../concepts/approvals.md), and [audit](../concepts/audit.md) record.

## Common patterns

- **Tenant scoping.** Set `tenant_id` on each credential; the orchestrator uses `principal.scope()` to ensure tenant data never leaks across deployments.
- **Role-based route checks.** `OPERATOR` runs graphs, `REVIEWER` resolves approvals, `ADMIN` mutates config — mix roles on a credential when one subject needs more than one capability.
- **Env-driven config in CI.** `ServiceAuthConfig.from_env()` reads `ZEROTH_SERVICE_API_KEYS_JSON` / `ZEROTH_SERVICE_BEARER_JSON`, which keeps secrets out of YAML.
- **JWT + API key side-by-side.** You can ship bearer auth for human SSO and keep a static API key around for automation; both paths yield the same `AuthenticatedPrincipal` shape.

## Pitfalls

1. **Missing `tenant_id` on a credential.** Defaults to `"default"`, which is fine for single-tenant demos but invisibly collides data across tenants in production.
2. **Shared secrets.** `StaticApiKeyCredential.secret` is compared via `hmac.compare_digest`; still, rotate them on schedule and never commit them.
3. **JWKS not reachable.** `BearerTokenConfig` requires `jwks_url` or an inline `jwks` — if the URL is unreachable at verify time, every bearer request fails.
4. **Forgetting required claims.** `roles`, `tenant_id`, and `workspace_id` on a JWT are read from `claims` and passed through verbatim; missing claims silently produce an `OPERATOR`-less principal that cannot run graphs.
5. **Assuming `ActorIdentity` carries raw tokens.** It does not — only `subject`, `auth_method`, `roles`, and scope. Raw `claims` stay on the request-bound `AuthenticatedPrincipal`.

## Reference cross-link

- Python API: [`zeroth.core.identity`](../reference/python-api.md#identity), [`zeroth.core.service.auth`](../reference/python-api.md#service-auth)
- Related: [Concept: identity](../concepts/identity.md), [Concept: service](../concepts/service.md), [Usage Guide: approvals](approvals.md), [Tutorial: governance walkthrough](../tutorials/governance-walkthrough.md).
