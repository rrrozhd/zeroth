"""Legacy import path for the webhook service domain.

Webhooks live in :mod:`zeroth.service.webhooks`; this package republishes
the same objects for compatibility. Import from the canonical location
instead (see docs/backend-import-migration.md).

The optional-dependency guards mirror the canonical package:
``delivery`` needs ``httpx``, so ``WebhookDeliveryWorker`` and
``WebhookService`` stay optional exports.
"""

import contextlib

from zeroth.service.webhooks.models import (
    DeliveryStatus,
    EscalationAction,
    WebhookDeadLetter,
    WebhookDelivery,
    WebhookEventPayload,
    WebhookEventType,
    WebhookSubscription,
)
from zeroth.service.webhooks.repository import WebhookRepository
from zeroth.service.webhooks.signing import sign_payload

with contextlib.suppress(ImportError):
    from zeroth.service.webhooks.delivery import WebhookDeliveryWorker  # noqa: F401

with contextlib.suppress(ImportError):
    from zeroth.service.webhooks.service import WebhookService  # noqa: F401

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
