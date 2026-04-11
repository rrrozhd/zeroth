# Execution units: usage guide

## Overview

This guide shows how to define an execution unit, register it, and wire it into a graph as an `ExecutableUnitNode`. The subsystem is described in the [execution units concept page](../concepts/execution-units.md); it is what lets your graphs call deterministic code — data transforms, fetchers, validators — alongside LLM-powered [agents](../concepts/agents.md). The moving parts you touch are an `ExecutableUnitManifest`, an `ExecutableUnitRegistry`, and an `ExecutableUnitRunner` that the orchestrator invokes on each visit.

## Minimal example

```python
import asyncio
from types import SimpleNamespace
from typing import Any

from zeroth.core.graph import (
    ExecutableUnitNode,
    ExecutableUnitNodeData,
)


class EchoRunner:
    """ExecutableUnitRunner-shaped stub that echoes its input payload."""

    async def run(self, manifest_ref: str, input_payload: Any) -> SimpleNamespace:
        return SimpleNamespace(
            output_data=dict(input_payload) if isinstance(input_payload, dict) else {},
            audit_record={"manifest_ref": manifest_ref},
        )


async def main() -> None:
    node = ExecutableUnitNode(
        node_id="echo",
        graph_version_ref="demo:1",
        executable_unit=ExecutableUnitNodeData(
            manifest_ref="unit://echo",
            execution_mode="native",
        ),
    )

    runner = EchoRunner()
    result = await runner.run(node.executable_unit.manifest_ref, {"message": "hi"})
    print(result.output_data)


asyncio.run(main())
```

## Common patterns

- **Native Python unit** — use `NativeUnitManifest` plus a `PythonRuntimeAdapter` for in-process calls when you trust the code and want zero-overhead dispatch.
- **Wrapped command** — use `WrappedCommandUnitManifest` with `CommandRuntimeAdapter` to shell out to an existing CLI (`jq`, `curl`, a bespoke binary) without rewriting it.
- **Project archive** — use `ProjectUnitManifest` to ship a whole project directory and execute it under the sandbox; useful for "bring your own repo" workflows.
- **Digest-pinned integrity** — compute `compute_manifest_digest()` at registration time and check it through `AdmissionController` so a tampered manifest never runs.

## Pitfalls

1. **Skipping validation** — always run the manifest through `ExecutableUnitValidator` before registering; a malformed `RunConfig` will fail deep inside the runner with a much worse error.
2. **Loose sandbox config** — the default `SandboxConfig` is permissive; set explicit `ResourceConstraints` (CPU, memory, timeout) for anything running untrusted code.
3. **Unchecked output extraction** — if you pick `output_extraction_strategy="json_stdout"`, the unit *must* print valid JSON on stdout; anything else raises `OutputExtractionError`.
4. **Blocking calls in `async def run`** — wrap synchronous work in `asyncio.to_thread()` so the orchestrator's event loop stays responsive.
5. **Missing `ExecutableUnitNotFoundError` handling** — calling a `manifest_ref` that was never registered raises this; surface it to the user rather than letting the orchestrator log a `NodeDispatcherError`.

## Reference cross-link

See the [Python API reference for `zeroth.core.execution_units`](../reference/python-api.md#zerothcoreexecution_units) (generated in Phase 32).
