# How to use mappings

## Overview

Mappings describe how data flows along a graph edge. Instead of hand-writing adapter code between nodes, you declare a list of small operations — passthrough, rename, constant, default — and Zeroth applies them when the orchestrator traverses the edge. This guide shows how to declare a mapping between two nodes and apply it to real data.

## Minimal example

```python
from zeroth.core.mappings import (
    EdgeMapping,
    MappingExecutor,
    PassthroughMappingOperation,
    RenameMappingOperation,
    ConstantMappingOperation,
    DefaultMappingOperation,
)

# Declare the mapping on an edge from "extract" -> "summarise"
mapping = EdgeMapping(
    edge_id="extract__to__summarise",
    operations=[
        PassthroughMappingOperation(target_path="text", source_path="body"),
        RenameMappingOperation(target_path="source_url", source_path="url"),
        ConstantMappingOperation(target_path="language", value="en"),
        DefaultMappingOperation(
            target_path="max_tokens",
            source_path="limits.max_tokens",
            default_value=512,
        ),
    ],
)

upstream_output = {"body": "Hello world", "url": "https://example.com"}
downstream_input = MappingExecutor().apply(mapping, upstream_output)
# -> {"text": "Hello world", "source_url": "https://example.com",
#     "language": "en", "max_tokens": 512}
```

In a real graph you attach the `EdgeMapping` to the edge definition; the orchestrator invokes `MappingExecutor` automatically during traversal.

## Common patterns

- **Passthrough-only edges** — when two nodes already share the same schema, a single `PassthroughMappingOperation` per field is enough. Prefer this over `RenameMappingOperation` when the names already match.
- **Constant injection for modes/flags** — use `ConstantMappingOperation` to pin a downstream field (e.g. `"language": "en"`) without exposing it in the upstream output contract.
- **Defaults for optional inputs** — use `DefaultMappingOperation` with a `source_path` of `None` to inject a default that upstream nodes never have to know about.
- **Dotted paths** — every `source_path` and `target_path` supports dotted notation to walk into nested dicts; use it to unpack structured upstream outputs.

## Pitfalls

1. **Silent field loss.** If you forget to declare an operation for a field the downstream contract requires, validation fails at runtime — not at graph load. Run `MappingValidator` in tests to catch missing fields early.
2. **Mixing `constant` and `default` semantics.** `ConstantMappingOperation` always overrides; `DefaultMappingOperation` only fills in when the source is missing. Using constant where you meant default hides upstream values.
3. **Non-JSON-serialisable constants.** Mapping operations are Pydantic models; any value you put into `ConstantMappingOperation.value` must round-trip through JSON to be persistable in a run snapshot.
4. **Forgetting discriminator.** `MappingOperation` is a discriminated union; each subclass has a `Literal` `operation` tag. When constructing from a dict, always include `"operation": "passthrough"` (etc.).
5. **Editing vs versioning.** Treat mappings as versioned alongside their edges — editing a mapping in place invalidates old run snapshots.

## Reference cross-link

- [Python API reference — `zeroth.core.mappings`](../reference/python-api.md)
