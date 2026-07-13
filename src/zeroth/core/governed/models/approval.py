from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Internal helper to utcnow."""
    return datetime.now(timezone.utc)


class ApprovalDecisionType(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalRequest(BaseModel):
    request_id: str
    run_id: str
    workflow_name: str
    step_name: str
    executor_name: str
    reason: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    metadata: dict[str, str] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    decision: ApprovalDecisionType
    decided_by: str | None = None
    reason: str | None = None
    decided_at: datetime = Field(default_factory=_utcnow)
