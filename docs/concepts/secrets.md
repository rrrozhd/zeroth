# Secrets

## What it is

The `zeroth.core.secrets` subsystem is Zeroth's abstraction for turning symbolic
**secret references** (e.g. `OPENAI_API_KEY`, `STRIPE_WEBHOOK_SECRET`) into
concrete string values at execution time — plus a matching **redactor** that
strips those values out of logs and traces.

## Why it exists

Graphs, execution units, and deployments are authored declaratively and stored
in version control. They must not contain literal credentials. At the same
time, execution units need real API keys to call LLMs and external tools, and
the runtime must guarantee that whatever gets pulled in does not leak back out
through logs, error messages, or the audit trail. The secrets module is the
single place where those two needs are reconciled: one interface to fetch, one
interface to scrub.

## Where it fits

Secrets sit between the [service bootstrap](service.md) — which wires a
concrete `SecretProvider` into the orchestrator — and every execution unit
that declares an `EnvironmentVariable` with a `secret_ref`. Identity and
per-tenant scoping (see [identity](../how-to/secrets.md#common-patterns))
decide *which* provider a request is allowed to read from. The observability
layer calls `SecretRedactor` before writing any run artifact.

## Key types

- **`SecretProvider`** — Protocol with `resolve(ref) -> str | None` and
  `resolve_many(refs) -> dict[str, str]`. Implement to plug in Vault, AWS
  Secrets Manager, GCP Secret Manager, etc.
- **`EnvSecretProvider`** — Default provider backed by a `Mapping[str, str]`
  (typically `os.environ`). The zero-config choice for local dev.
- **`SecretResolver`** — Walks a list of `EnvironmentVariable` models, pulls
  their `secret_ref`s through a provider, and returns the concrete
  `{name: value}` dict the execution unit runner needs.
- **`SecretRedactor`** — Given a set of known secret values, masks every
  occurrence inside arbitrary strings before they reach logs or the audit
  store.

## See also

- Usage Guide: [how-to/secrets](../how-to/secrets.md)
- Related: [service](service.md), [identity](identity.md)
