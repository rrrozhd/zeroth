# Inject a secret into an execution unit

## What this recipe does
Resolves a list of `EnvironmentVariable` records — some static, some
referencing secret refs — into a concrete env dict via `SecretResolver`,
then uses `SecretRedactor` to scrub the resolved values out of an audit
payload before it's persisted.

## When to use
- An executable unit needs API keys, DB passwords, or tokens at run
  time, and you don't want those values baked into the manifest.
- You're writing audit records that include tool inputs and want
  automatic redaction of any value that came from a secret provider.
- You're swapping between local env-backed secrets for tests and a
  vault-backed provider in production.

## When NOT to use
- The value is not secret — use `EnvironmentVariable(value=...)`
  directly and skip the resolver.
- You need dynamic per-request secrets rotated in-flight — build a
  custom `SecretProvider` implementation instead of `EnvSecretProvider`.

## Recipe
```python
--8<-- "23_secrets_and_sandbox.py"
```

## How it works
`SecretResolver.resolve_environment_variables` walks the env var list,
fetches any `secret_ref` values in one batch via
`SecretProvider.resolve_many`, and raises `KeyError` if any are
missing. The resolver remembers every value it handed out; calling
`resolver.redactor()` returns a `SecretRedactor` seeded with those
values so recursive `redact(payload)` calls replace each one with a
`[REDACTED:<ref>]` marker.

## See also
- [Usage Guide: secrets](../secrets.md)
- [Concept: secrets](../../concepts/secrets.md)
- [Concept: execution-units](../../concepts/execution-units.md)
