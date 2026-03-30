"""Data models for the memory connector system.

These Pydantic models define the shapes of data used when setting up,
resolving, and using memory connectors. Think of them as the "contracts"
that different parts of the memory system agree on.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConnectorScope(StrEnum):
    """How widely a memory connector's data is shared.

    - RUN: data lives only within a single run (execution).
    - THREAD: data is shared across runs in the same conversation thread.
    - SHARED: data is shared globally across all runs and threads.
    """

    RUN = "run"
    THREAD = "thread"
    SHARED = "shared"


class ConnectorManifest(BaseModel):
    """Describes how a memory connector is configured.

    This is the "ID card" for a connector -- it tells the system what type
    of connector to use, what scope it operates at, and any extra config.
    """

    model_config = ConfigDict(extra="forbid")

    connector_type: str
    scope: ConnectorScope
    instance_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class MemoryContext(BaseModel):
    """Runtime context passed to a memory connector during read/write.

    Contains everything a connector needs to know about the current
    execution environment -- which run, thread, node, and scope it's
    operating in.
    """

    model_config = ConfigDict(extra="forbid")

    memory_ref: str
    instance_id: str
    scope: ConnectorScope
    run_id: str | None = None
    thread_id: str | None = None
    node_id: str | None = None


class ResolvedMemoryBinding(BaseModel):
    """A fully resolved, ready-to-use memory binding.

    Combines the connector instance, its manifest (configuration), and
    the execution context so the system can immediately read/write memory
    without any further lookups.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    memory_ref: str
    manifest: ConnectorManifest
    connector: Any
    context: MemoryContext
