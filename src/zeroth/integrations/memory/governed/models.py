"""Memory models: MemoryScope enum and MemoryEntry Pydantic model."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from zeroth.contracts.governed.models.common import JSONValue
from zeroth.contracts.governed.models.memory import MemoryScope


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryEntry(BaseModel):
    key: str
    value: JSONValue
    scope: MemoryScope
    scope_target: str
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)
