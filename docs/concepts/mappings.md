# Mappings

## What it is

A **mapping** is a declarative description of how data moves along an edge between two nodes in a Zeroth graph. Each mapping is a list of small operations — copy this field, rename that one, set this to a constant, fall back to a default — that together shape the downstream node's input from the upstream node's output.

## Why it exists

Without mappings, every node has to know the exact shape of every upstream node's output. That couples nodes together and makes graphs brittle.

Mappings solve this by making the edge — not the node — responsible for translating between data shapes. A node declares the contract it expects, and the edge's mapping adapts whatever the predecessor produced into that contract.

This keeps nodes reusable and makes graphs composable: you can rewire a graph by changing mappings, without touching node code. It also gives you a single place to look when debugging why a downstream node saw the wrong input.

## Where it fits

Mappings live on graph edges. When the [orchestrator](orchestrator.md) runs a graph, the [graph](graph.md) engine invokes a `MappingExecutor` for every edge it traverses.

The executor reads the upstream node's output, applies each operation in order, validates the result against the downstream node's [contract](contracts.md), and hands the result to the next node. Mappings are also the place where constants, defaults, and renames get enforced — keeping nodes themselves pure and stateless.

## Key types

All of these live under `zeroth.core.mappings`:

- **`EdgeMapping`** — the full mapping attached to one edge: a list of operations plus the edge identity.
- **`MappingOperation`** — a discriminated union of all operation types. Pick one per field you want to produce.
- **`PassthroughMappingOperation`** — copy a field from source to target using the same path.
- **`RenameMappingOperation`** — copy a field but rename it on the way through.
- **`ConstantMappingOperation`** — always write a fixed value, ignoring upstream output entirely.
- **`DefaultMappingOperation`** — copy from source, or fall back to a default if the source is missing.
- **`MappingExecutor`** — applies an `EdgeMapping` to real data at runtime.
- **`MappingValidator` / `MappingValidationError`** — static validation for mapping definitions.

Every operation subclass inherits from `MappingOperationBase`, which guarantees `extra="forbid"` on the Pydantic config — so unrecognised fields are rejected, not silently dropped. This is what makes mapping definitions safe to persist and round-trip through JSON.

## See also

- [Usage Guide: mappings](../how-to/mappings.md) — declare a mapping between two nodes.
- [Concept: graph](graph.md) — where mappings live.
- [Concept: contracts](contracts.md) — the target schema that mappings must satisfy.
