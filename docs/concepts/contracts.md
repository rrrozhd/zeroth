# Contracts

## What it is

A **contract** in Zeroth is a named, versioned Pydantic model registered in a central registry so that multiple parts of the system — nodes, tools, mappings, and external callers — can agree on the exact shape of some piece of data. Contracts are stored in a database, looked up by name and version, and can be resolved back to their original Python class.

## Why it exists

Plain Pydantic models are great at validating data, but they live inside one Python process and one codebase. Multi-agent systems need shared agreements about data shapes that outlive any single process: the same tool input schema has to be understood by the runtime, the governance layer, the audit trail, and any future replay.

Contracts give you that shared, versioned vocabulary. When a tool's I/O schema changes, you register a new *version* rather than editing the old one — so existing runs and bindings keep working, and the audit log can always reconstruct what a historical run expected.

A contract is therefore more than "a Pydantic model with a name". It is a *durable*, *resolvable*, *versioned* reference that any part of the system can depend on without importing a specific Python class.

## Where it fits

Contracts sit underneath nodes and mappings. A node declares input and output contracts; the orchestrator uses those to validate data before and after each step.

The [mappings](mappings.md) subsystem validates that an edge's operations produce data matching the downstream contract. [Agents](agents.md) and tools bind to contracts via `ToolContractBinding` and `StepContractBinding` so that the runtime can enforce type safety and the governance plane can reason about what data a tool is allowed to see. Contracts are persisted via [storage](storage.md).

## Key types

All of these live under `zeroth.core.contracts`:

- **`ContractRegistry`** — the async, database-backed store where contracts are registered and looked up.
- **`ContractReference`** — a lightweight `(name, version)` pointer used to refer to a contract without loading it.
- **`ContractVersion`** — the full record for one version: name, version number, model path, JSON schema, metadata, created timestamp.
- **`ToolContractBinding`** — how a GovernAI tool connects to its input and output contracts.
- **`StepContractBinding`** — how a workflow step binds its inputs and outputs to contracts.
- **`ContractNotFoundError` / `ContractRegistryError`** — raised when lookup or registration fails.

A contract therefore has three facets: its **schema** (the JSON schema stored in the database), its **class** (the original Pydantic model, resolvable from `model_path`), and its **reference** (the `(name, version)` pair used by callers who only need to point at it).

## Contracts vs. ordinary Pydantic models

It is important to be clear about what contracts are *not*. An ordinary Pydantic model is a local Python class; a contract is a *registered, versioned, persisted* reference to such a class. You can build a working Zeroth application with plain Pydantic models and no contracts at all. You adopt contracts the moment you need two components — possibly in two processes, possibly built months apart — to agree on a schema without importing each other's code.

## See also

- [Usage Guide: contracts](../how-to/contracts.md) — define a contract and attach it to a node.
- [Concept: mappings](mappings.md) — edges validate against contracts.
- [Concept: agents](agents.md) — agents declare contract-typed I/O.
