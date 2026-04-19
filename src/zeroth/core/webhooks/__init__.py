"""Webhook delivery system for Zeroth platform.

Provides webhook subscription management, delivery lifecycle tracking,
HMAC-SHA256 payload signing, and dead-letter handling.
"""

import contextlib

from zeroth.core.webhooks.models import (
    DeliveryStatus,
    EscalationAction,
    WebhookDeadLetter,
    WebhookDelivery,
    WebhookEventPayload,
    WebhookEventType,
    WebhookSubscription,
)
from zeroth.core.webhooks.repository import WebhookRepository
from zeroth.core.webhooks.signing import sign_payload

with contextlib.suppress(ImportError):
    from zeroth.core.webhooks.delivery import WebhookDeliveryWorker  # noqa: F401

with contextlib.suppress(ImportError):
    from zeroth.core.webhooks.service import WebhookService  # noqa: F401

__all__ = [
    "DeliveryStatus",
    "EscalationAction",
    "WebhookDeadLetter",
    "WebhookDelivery",
    "WebhookDeliveryWorker",
    "WebhookEventPayload",
    "WebhookEventType",
    "WebhookRepository",
    "WebhookService",
    "WebhookSubscription",
    "sign_payload",
]
