"""Data models for the webhook delivery system.

Defines event types, subscription records, delivery tracking, dead-letter
entries, and escalation actions used throughout the webhook subsystem.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def _new_id() -> str:
    """Generate a new unique ID string."""
    return uuid4().hex


class WebhookEventType(StrEnum):
    """Types of events that can trigger webhook deliveries."""

    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    APPROVAL_ESCALATED = "approval.escalated"


class DeliveryStatus(StrEnum):
    """Lifecycle states of a webhook delivery attempt."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class EscalationAction(StrEnum):
    """Actions to take when an approval SLA expires."""

    DELEGATE = "delegate"
    AUTO_REJECT = "auto_reject"
    ALERT = "alert"


class WebhookSubscription(BaseModel):
    """A registered webhook endpoint for a specific deployment.

    Tracks which event types should be delivered to which URL, along with
    the shared secret used for HMAC signing.
    """

    model_config = ConfigDict(extra="forbid")

    subscription_id: str = Field(default_factory=_new_id)
    deployment_ref: str
    tenant_id: str = "default"
    target_url: str
    secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    event_types: list[WebhookEventType]
    active: bool = True
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class WebhookDelivery(BaseModel):
    """Tracks a single webhook delivery attempt.

    Created when an event fires. Moves through PENDING -> DELIVERED or
    PENDING -> FAILED -> ... -> DEAD_LETTER if all retries are exhausted.
    """

    model_config = ConfigDict(extra="forbid")

    delivery_id: str = Field(default_factory=_new_id)
    subscription_id: str
    event_type: WebhookEventType
    event_id: str = Field(default_factory=_new_id)
    payload_json: str
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempt_count: int = 0
    max_attempts: int = 5
    next_attempt_at: datetime = Field(default_factory=_utc_now)
    last_error: str | None = None
    last_status_code: int | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class WebhookDeadLetter(BaseModel):
    """A delivery that has been moved to the dead-letter queue.

    Preserves all delivery metadata for later inspection or manual retry.
    """

    model_config = ConfigDict(extra="forbid")

    dead_letter_id: str = Field(default_factory=_new_id)
    delivery_id: str
    subscription_id: str
    event_type: WebhookEventType
    event_id: str
    payload_json: str
    attempt_count: int
    last_error: str | None = None
    last_status_code: int | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    dead_lettered_at: datetime = Field(default_factory=_utc_now)


class WebhookEventPayload(BaseModel):
    """Standard envelope for webhook event data.

    Wraps the event-specific data with common metadata fields for
    consistent parsing on the receiving end.
    """

    model_config = ConfigDict(extra="forbid")

    event_type: WebhookEventType
    event_id: str = Field(default_factory=_new_id)
    timestamp: datetime = Field(default_factory=_utc_now)
    deployment_ref: str
    tenant_id: str
    data: dict[str, Any]
