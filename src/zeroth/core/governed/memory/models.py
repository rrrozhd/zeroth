"""Memory models: MemoryScope enum and MemoryEntry Pydantic model."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from zeroth.core.governed.models.common import JSONValue


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryScope(str, Enum):
    RUN = "run"
    THREAD = "thread"
    SHARED = "shared"


class MemoryEntry(BaseModel):
    key: str
    value: JSONValue
    scope: MemoryScope
    scope_target: str
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)
