"""Inject a secret into an execution unit — example for docs/how-to/cookbook/secret-injection.md.

Uses :class:`EnvSecretProvider` + :class:`SecretResolver` to turn a list
of :class:`EnvironmentVariable` records (some static, some referencing
secret refs) into a concrete dict of env vars suitable for a sandbox
run. Then demonstrates :class:`SecretRedactor` scrubbing the resolved
value out of an audit payload. Pure in-process.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    required_env: list[str] = []
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        print(f"SKIP: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 0

    from zeroth.core.execution_units.models import EnvironmentVariable
    from zeroth.core.secrets import EnvSecretProvider, SecretResolver

    # 1. Seed a provider with secret refs. In production this is the
    #    process environment or a vault-backed provider — here we inject
    #    the mapping directly so the example is hermetic.
    provider = EnvSecretProvider(
        environment={
            "secret/demo/api_key": "sk-demo-zeroth-cookbook-1234",
            "secret/demo/db_password": "super-secret-db-pw",
        }
    )
    resolver = SecretResolver(provider)

    # 2. Describe the env vars the execution unit expects: one plain value,
    #    two secret references.
    env_vars = [
        EnvironmentVariable(name="DEMO_MODE", value="cookbook"),
        EnvironmentVariable(name="DEMO_API_KEY", secret_ref="secret/demo/api_key"),
        EnvironmentVariable(name="DEMO_DB_PASSWORD", secret_ref="secret/demo/db_password"),
    ]

    resolved = resolver.resolve_environment_variables(env_vars)
    print("resolved env:")
    for key, value in resolved.items():
        print(f"  {key}={value}")
    assert resolved["DEMO_API_KEY"].startswith("sk-demo-")

    # 3. Build a redactor from the resolver's known secrets. Any audit
    #    payload containing a resolved value gets replaced with a
    #    [REDACTED:<ref>] marker before it's written to the audit store.
    redactor = resolver.redactor()
    audit_payload = {
        "tool": "http-post",
        "headers": {"Authorization": f"Bearer {resolved['DEMO_API_KEY']}"},
        "body": f"password={resolved['DEMO_DB_PASSWORD']}",
    }
    redacted = redactor.redact(audit_payload)
    print("redacted audit payload:")
    print(f"  {redacted}")
    assert "sk-demo-" not in str(redacted)
    assert "super-secret-db-pw" not in str(redacted)
    print("secret-injection demo OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
