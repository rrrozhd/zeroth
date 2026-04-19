# Using secrets

## Overview

You almost never hand-roll secret lookups in Zeroth. Instead, you declare
`EnvironmentVariable` entries on an execution unit with `secret_ref` set to a
symbolic name, and the runtime resolves them through a `SecretProvider` that
the service bootstrap wired in. This page shows how to pick or implement a
provider, how the resolver is called, and how to keep values out of logs.

## Minimal example

```python
from zeroth.core.secrets import (
    EnvSecretProvider,
    SecretRedactor,
    SecretResolver,
)
from zeroth.core.execution_units.models import EnvironmentVariable

# 1. Pick a provider. EnvSecretProvider is the default for local dev.
provider = EnvSecretProvider({"OPENAI_API_KEY": "sk-test-123"})

# 2. Wrap it in a resolver and feed it an EU's env var declarations.
resolver = SecretResolver(provider)
env = resolver.resolve_environment_variables(
    [EnvironmentVariable(name="OPENAI_API_KEY", secret_ref="OPENAI_API_KEY")]
)
assert env == {"OPENAI_API_KEY": "sk-test-123"}

# 3. Redact before logging.
redactor = SecretRedactor(known_values={"sk-test-123"})
print(redactor.redact("calling LLM with sk-test-123"))  # -> "calling LLM with ***"
```

## Common patterns

- **Env-backed dev, Vault-backed prod** — Both environments wire the same
  `SecretResolver` but differ only in which `SecretProvider` implementation
  is constructed inside `bootstrap_service`.
- **Per-tenant scoping** — Wrap your base provider in a decorator that checks
  the incoming identity before delegating to `resolve()`.
- **Bulk resolution** — Always prefer `resolve_many()` in hot paths; it lets
  custom providers batch a single network round-trip per execution unit.
- **Always redact** — Register every resolved value with a `SecretRedactor`
  at the same moment you resolve it, so no code path can log the raw value.

## Pitfalls

1. **Plaintext `.env` files** — `EnvSecretProvider` is fine for dev but is
   not a production secret store. Do not commit `.env` files.
2. **Forgetting to redact** — Resolving a secret without also registering
   it with a `SecretRedactor` means it *will* eventually land in a log line.
3. **Rotation without cache invalidation** — If your custom provider caches,
   bound the TTL; otherwise rotated keys stay stale until process restart.
4. **Scoping mistakes** — A provider that ignores tenant identity can leak
   tenant A's secrets to tenant B. Always enforce scoping inside `resolve()`.
5. **Missing refs** — `SecretResolver.resolve_environment_variables` raises
   `KeyError` when a `secret_ref` cannot be resolved. Handle it at bootstrap
   so deployments fail loudly instead of running half-configured.

## Reference cross-link

See the [Python API reference for `zeroth.core.secrets`](../reference/python-api/secrets.md).

Related guides: [service how-to](service.md) · [concepts/secrets](../concepts/secrets.md).
