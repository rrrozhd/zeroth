"""23 — Secrets resolved through a real SecretResolver + in-process sandbox run.

What this shows
---------------
Two real primitives stitched together:

* :class:`SecretResolver` + :class:`EnvSecretProvider` turn a list of
  :class:`EnvironmentVariable` records (some plain, some ``secret_ref``)
  into a concrete env dict — the same path the runner uses when
  building the env for a sandboxed unit.
* :class:`SandboxManager` runs a short ``python -c`` command inside
  the ``local`` sandbox backend with that env. The env is scrubbed to
  an allowlist and the secret arrives inside the subprocess.
* :class:`SecretRedactor` (via ``resolver.redactor()``) is the same
  redactor the audit pipeline uses to strip secret values out of
  payloads before they are persisted.

Run
---
    uv run python examples/23_secrets_and_sandbox.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import os
import sys

from zeroth.core.execution_units import (
    EnvironmentVariable,
    SandboxBackendMode,
    SandboxConfig,
    SandboxManager,
)
from zeroth.core.secrets import EnvSecretProvider, SecretResolver


def main() -> int:
    # 1. Provider seeded with two secret refs. In production this is a
    #    vault-backed provider; here we inject a dict so the example is
    #    hermetic. No real secrets live in the file.
    provider = EnvSecretProvider(
        environment={
            "secret/demo/api_key": "sk-demo-zeroth-23-example-1234",
            "secret/demo/db_password": "p@ssw0rd-demo",
        }
    )
    resolver = SecretResolver(provider)

    # 2. EnvironmentVariable records mix plain values and secret refs.
    #    This is exactly the shape an ExecutableUnitManifest carries in
    #    its ``environment_variables`` field.
    env_vars = [
        EnvironmentVariable(name="DEMO_MODE", value="cookbook"),
        EnvironmentVariable(name="DEMO_API_KEY", secret_ref="secret/demo/api_key"),
        EnvironmentVariable(name="DEMO_DB_PASSWORD", secret_ref="secret/demo/db_password"),
    ]
    resolved = resolver.resolve_environment_variables(env_vars)
    print("resolved env (full):")
    for key, value in resolved.items():
        print(f"  {key}={value}")

    # 3. Run a real command inside SandboxManager's local backend. The
    #    allowlist keeps PATH; the overlay adds our demo env on top.
    manager = SandboxManager(
        base_env={
            "HOME": os.environ.get("HOME", "/tmp"),
            "PATH": os.environ.get("PATH", ""),
        },
        config=SandboxConfig(backend=SandboxBackendMode.LOCAL),
    )
    result = manager.run(
        ["python", "-c", "import os; print(os.environ.get('DEMO_API_KEY', 'missing'))"],
        allowed_env_keys=["PATH"],
        overlay_env=resolved,
        cache_identity={"example": "secrets-sandbox"},
        timeout_seconds=10,
    )
    print(f"\nsandbox returncode={result.returncode} stdout={result.stdout.strip()!r}")
    assert result.returncode == 0
    assert result.stdout.strip() == "sk-demo-zeroth-23-example-1234"

    # 4. Redactor strips the resolved secret out of an audit payload
    #    before it reaches the audit store. This is the same redactor
    #    the framework uses automatically in production.
    audit_payload = {
        "tool": "http-post",
        "headers": {"Authorization": f"Bearer {resolved['DEMO_API_KEY']}"},
        "body": f"password={resolved['DEMO_DB_PASSWORD']}",
    }
    redacted = resolver.redactor().redact(audit_payload)
    print("\nredacted audit payload (what the audit store sees):")
    print(f"  {redacted}")
    assert "sk-demo-zeroth-23-example-1234" not in str(redacted)
    assert "p@ssw0rd-demo" not in str(redacted)

    print("\nsecrets-and-sandbox demo OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
