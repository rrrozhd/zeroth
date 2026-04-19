"""Data models for the approval system.

Defines the enums that represent approval states and decisions, plus the
Pydantic models that carry approval data between the service layer, the
database, and the API.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from zeroth.core.identity import ActorIdentity


def _utc_now() -> datetime:
    """Return the current time in UTC. Used as a default factory for timestamp fields."""
    return datetime.now(UTC)


def _new_id() -> str:
    """Generate a new unique ID string. Used as a default factory for ID fields."""
    return uuid4().hex


class HumanInteractionType(StrEnum):
    """The kind of interaction the system is requesting from a human.

    For example, "approval" means the system needs a yes/no decision,
    while "clarification" means it needs more information to proceed.
    """

    APPROVAL = "approval"
    CLARIFICATION = "clarification"
    REQUEST_INPUT = "request_input"
    NOTIFICATION = "notification"


class ApprovalDecision(StrEnum):
    """The possible choices a human reviewer can make on an approval request.

    APPROVE — accept as-is. REJECT — deny the action. EDIT_AND_APPROVE — modify
    the proposed payload and then accept it.
    """

    APPROVE = "approve"
    REJECT = "reject"
    EDIT_AND_APPROVE = "edit_and_approve"


class ApprovalStatus(StrEnum):
    """Whether an approval request is still waiting or has been answered.

    PENDING means no decision yet; RESOLVED means a human has responded.
    """

    PENDING = "pending"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class ApprovalResolution(BaseModel):
    """Holds the details of how an approval was resolved.

    Stores who made the decision, what they decided, any edits they made to
    the payload, and when the decision happened.
    """

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision
    actor: ActorIdentity
    edited_payload: dict[str, Any] | None = None
    resolved_at: datetime = Field(default_factory=_utc_now)


class ApprovalRecord(BaseModel):
    """The main approval object that tracks a single approval request.

    Created when an agent workflow hits a node that requires human sign-off.
    Contains everything a reviewer needs: a summary, rationale, the proposed
    payload, which actions are allowed, and (once answered) the resolution.
    """

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(default_factory=_new_id)
    run_id: str
    thread_id: str | None = None
    node_id: str
    graph_version_ref: str
    deployment_ref: str
    tenant_id: str = "default"
    workspace_id: str | None = None
    requested_by: ActorIdentity | None = None
    interaction_type: HumanInteractionType = HumanInteractionType.APPROVAL
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_decision: ApprovalDecision = ApprovalDecision.APPROVE
    # Which decisions the reviewer is allowed to pick (e.g., only approve/reject, no edits)
    allowed_actions: list[ApprovalDecision] = Field(default_factory=list)
    summary: str
    rationale: str
    context_excerpt: dict[str, Any] = Field(default_factory=dict)
    proposed_payload: dict[str, Any] | None = None
    urgency_metadata: dict[str, Any] = Field(default_factory=dict)
    resolution_schema_ref: str | None = None
    resolution: ApprovalResolution | None = None
    sla_deadline: datetime | None = None
    escalation_action: str | None = None
    escalated_from_id: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
