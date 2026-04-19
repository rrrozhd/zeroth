# Sandbox a tool call

## What this recipe does
Runs a command inside a `SandboxManager` with a scrubbed environment,
an allowlist of env keys, and an explicit overlay. The local-subprocess
backend needs no Docker, so the demo runs anywhere Python does.

## When to use
- You're invoking an `ExecutableUnitNode` whose manifest calls out to
  an external binary and you want it isolated from the parent env.
- You want deterministic, cache-keyed environment preparation so
  repeated runs reuse the same prepared sandbox.
- You need to restrict which env variables a tool sees (e.g. strip
  `AWS_*` keys before running untrusted code).

## When NOT to use
- The tool is pure Python and can run in-process — you save the
  subprocess overhead by calling it directly.
- You need full container isolation — use the Docker backend
  (`SandboxBackendMode.DOCKER`) instead of `LOCAL`.

## Recipe
```python
--8<-- "23_secrets_and_sandbox.py"
```

## How it works
`SandboxManager.prepare_environment` computes a cache key from the
runtime metadata and the allowlist, then builds (or reuses) a
`SandboxEnvironment` containing only the allowed variables plus the
overlay. `SandboxManager.run` dispatches the command through the
configured backend — local subprocess here — and returns a
`SandboxExecutionResult` with stdout, stderr, exit code, and timing.

## See also
- [Usage Guide: execution-units](../execution-units.md)
- [Concept: execution-units](../../concepts/execution-units.md)
- [Concept: guardrails](../../concepts/guardrails.md)
