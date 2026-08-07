"""Contract-owned vocabulary for governed tool registration.

The registry persists tool contracts, so the vocabulary those records use is
owned here rather than by the runtime tool primitives. ``ExecutionPlacement``
is the placement literal stored on ``ToolContractBinding``;
``zeroth.runtime.agents.tooling.base`` republishes it for the tool classes
themselves. ``RegistrableTool`` is the structural surface
``ContractRegistry.register_tool`` reads off a governed tool — the registry
never executes a tool, so it depends on this protocol instead of the runtime
``Tool`` class.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel

ExecutionPlacement = Literal["local_only", "remote_only", "local_or_remote"]


class RegistrableTool(Protocol):
    """The attributes ``register_tool`` reads when persisting a tool's contracts."""

    name: str
    remote_name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    capabilities: list[str]
    side_effect: bool
    timeout_seconds: float | None
    requires_approval: bool
    tags: list[str]
    executor_type: str
    execution_placement: ExecutionPlacement
