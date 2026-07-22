from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from zeroth.contracts.governed.models.approval import ApprovalRequest
from zeroth.contracts.governed.models.common import RunStatus


def _utcnow() -> datetime:
    """Internal helper to utcnow."""
    return datetime.now(UTC)


class RunState(BaseModel):
    run_id: str
    thread_id: str | None = None
    checkpoint_id: str | None = None
    parent_checkpoint_id: str | None = None
    epoch: int = 0
    workflow_name: str
    status: RunStatus = RunStatus.PENDING
    current_step: str | None = None
    completed_steps: list[str] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    channels: dict[str, Any] = Field(default_factory=dict)
    pending_approval: ApprovalRequest | None = None
    pending_interrupt_id: str | None = None
    started_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _default_thread_id(self) -> RunState:
        """Internal helper to default thread id."""
        if self.thread_id is None:
            self.thread_id = self.run_id
        return self

    def touch(self) -> None:
        """Touch."""
        self.updated_at = _utcnow()
