# How to use contracts

## Overview

Contracts let you register a Pydantic model as a named, versioned schema in a central registry, and then reference it by name from anywhere in your system — nodes, tools, mappings, and the governance plane. This guide shows how to define a contract, register it, and attach it to a workflow step.

## Minimal example

```python
import asyncio
from pydantic import BaseModel

from zeroth.core.contracts import (
    ContractRegistry,
    ContractReference,
    StepContractBinding,
)
from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase


class SummariseInput(BaseModel):
    text: str
    max_tokens: int = 512


class SummariseOutput(BaseModel):
    summary: str


async def main() -> None:
    db = AsyncSQLiteDatabase(path=":memory:")
    await db.connect()
    registry = ContractRegistry(db)
    await registry.initialize()

    # Register both contracts (version numbers are assigned automatically)
    in_ref = await registry.register("summarise.input", SummariseInput)
    out_ref = await registry.register("summarise.output", SummariseOutput)

    # Attach the contracts to a workflow step
    binding = StepContractBinding(
        step_name="summarise",
        input_contract=in_ref,
        output_contract=out_ref,
    )
    print(binding)

    # Later, resolve a contract by name/version to get its schema or class back
    resolved = await registry.get(ContractReference(name="summarise.input"))
    print(resolved.json_schema)


asyncio.run(main())
```

## Common patterns

- **Name contracts after the concept, not the call site.** Prefer `customer.profile.v1` over `get_profile_input` so the same contract can be reused across nodes, tools, and APIs.
- **Register at startup.** Call `registry.register(...)` for every model your service uses during application bootstrap, not lazily — this gives you a fail-fast at boot if something is misconfigured.
- **Resolve by reference, not by class.** In downstream code, pass a `ContractReference(name=..., version=...)` instead of the Python class directly; this keeps runtime code decoupled from the class path.
- **Tool bindings.** For GovernAI tools, use `ToolContractBinding` instead of `StepContractBinding`; it captures extra metadata like execution placement and side-effect flags.

## Pitfalls

1. **Editing in place.** Never mutate a registered contract — always register a new version. Old runs reference old versions.
2. **Unstable `model_path`.** Contracts persist the dotted path to your Pydantic class. Renaming or moving a class breaks resolution; use `ContractTypeResolutionError` to detect this in tests.
3. **Forgetting to `await registry.initialize()`.** The registry lazily creates its schema; calling `register` first raises `ContractRegistryError`.
4. **Mixing pydantic v1 and v2 models.** Zeroth assumes pydantic v2. Older `BaseModel` subclasses can be registered but will not resolve correctly.
5. **Version drift between services.** If two services register different classes under the same `(name, version)`, whoever writes last wins. Make registration the responsibility of exactly one service.

## Reference cross-link

- [Python API reference — `zeroth.core.contracts`](../reference/python-api.md)
