# Execution units

## What it is

An **execution unit** is a packaged, governed piece of runnable code — Python module, wrapped shell command, or project archive — that a graph node can invoke as a deterministic step. The `zeroth.core.execution_units` subsystem provides the manifests, validators, sandbox, and runner that make it safe to call arbitrary code from inside a workflow.

## Why it exists

LLM agents need deterministic sidekicks: data transforms, database queries, shell-outs, fetches, file munging. Letting agents call raw Python functions undermines governance — there is no manifest, no integrity check, no resource limit, no audit trail. Execution units fix that by requiring every callable to be declared as an `ExecutableUnitManifest`, validated, digest-pinned for integrity, and executed under a `SandboxConfig` with explicit `ResourceConstraints`. The result is a step that behaves like a function to the graph author but like a container to the operator.

## Where it fits

Execution units are the non-LLM counterpart to [agents](agents.md) inside a [graph](graph.md). When the [orchestrator](orchestrator.md) visits an `ExecutableUnitNode`, it dispatches to `ExecutableUnitRunner`, which looks up the manifest in `ExecutableUnitRegistry`, runs the `AdmissionController` check, injects inputs, executes under a sandbox, extracts the output, and returns a result the orchestrator can feed into the next edge. [Conditions](conditions.md) then decide where the run goes next.

## Key types

- **`ExecutableUnitRunner`** — the entry point; the orchestrator calls `await runner.run(manifest_ref, input_payload)`.
- **`ExecutableUnitManifest`** — discriminated union of `NativeUnitManifest`, `WrappedCommandUnitManifest`, and `ProjectUnitManifest`, each describing a runnable artifact.
- **`ExecutableUnitRegistry`** — registry that stores manifests and resolves `manifest_ref` values at run time.
- **`SandboxManager` / `SandboxConfig`** — builds and enforces the isolated environment (native, subprocess, or Docker) the unit runs inside.
- **`AdmissionController`** — pre-execution guard that checks integrity digests and policy before a unit is allowed to run.

## See also

- [Usage Guide: execution units](../how-to/execution-units.md)
- [Concept: agents](./agents.md)
- [Concept: graph](./graph.md)
