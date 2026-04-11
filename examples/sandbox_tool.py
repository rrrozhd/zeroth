"""Sandbox a tool call — runnable example for docs/how-to/cookbook/sandbox-tool.md.

Uses :class:`zeroth.core.execution_units.SandboxManager` to execute a
command inside a prepared, cached sandbox environment. The default
``SandboxBackendMode.LOCAL`` runs the command in a local subprocess
with a scrubbed environment and an allowlist of env keys — no Docker
required, so the demo runs anywhere.
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

    from zeroth.core.execution_units import (
        SandboxBackendMode,
        SandboxConfig,
        SandboxManager,
    )

    # 1. Build a local-subprocess sandbox. Docker mode would swap backend=DOCKER.
    manager = SandboxManager(
        base_env={"HOME": os.environ.get("HOME", "/tmp"), "PATH": os.environ.get("PATH", "")},
        config=SandboxConfig(backend=SandboxBackendMode.LOCAL),
    )

    # 2. Prepare an environment with an allowlist and an explicit overlay.
    #    Anything not on the allowlist is stripped before the command runs.
    env = manager.prepare_environment(
        allowed_env_keys=["PATH"],
        overlay={"DEMO_TOOL": "zeroth-cookbook"},
        cache_identity={"example": "sandbox-tool"},
    )
    print(
        f"prepared cache_key={env.cache_key[:16]}… "
        f"DEMO_TOOL={env.variables['DEMO_TOOL']}"
    )

    # 3. Run a command inside the sandbox. Python itself is the most
    #    portable binary we can assume is on PATH.
    probe = "import os, sys; sys.stdout.write(os.environ.get('DEMO_TOOL', 'missing'))"
    result = manager.run(
        ["python", "-c", probe],
        allowed_env_keys=["PATH"],
        overlay_env={"DEMO_TOOL": "zeroth-cookbook"},
        cache_identity={"example": "sandbox-tool"},
        timeout_seconds=10,
    )
    print(
        f"sandbox returncode={result.returncode} "
        f"stdout={result.stdout.strip()!r} backend={result.backend}"
    )
    assert result.returncode == 0, f"sandbox command failed: {result.stderr}"
    assert result.stdout.strip() == "zeroth-cookbook"
    print("sandbox-tool demo OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
