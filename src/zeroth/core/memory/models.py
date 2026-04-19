"""Data models for the memory connector system.

These Pydantic models define the shapes of data used when setting up,
resolving, and using memory connectors. Uses GovernAI MemoryScope
instead of the old ConnectorScope.
"""

from __future__ import annotations

from typing import Any

from governai.memory.models import MemoryScope
from pydantic import BaseModel, ConfigDict, Field


class ConnectorManifest(BaseModel):
    """Describes how a memory connector is configured.

    This is the "ID card" for a connector -- it tells the system what type
    of connector to use, what scope it operates at, and any extra config.
    """

    model_config = ConfigDict(extra="forbid")

    connector_type: str
    scope: MemoryScope
    instance_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class ResolvedMemoryBinding(BaseModel):
    """A fully resolved, ready-to-use memory binding.

    Combines the connector instance and its manifest (configuration).
    The connector is already wrapped with ScopedMemoryConnector and
    (optionally) AuditingMemoryConnector, so callers just call
    read/write/delete/search directly.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    memory_ref: str
    manifest: ConnectorManifest
    connector: Any  # GovernAI MemoryConnector (wrapped with Scoped+Auditing)
