"""Legacy import location for the webhook api module.

The definitions now live in :mod:`zeroth.service.api.webhook_api`. This module republishes
the same objects, so the protected legacy import path keeps resolving to
identical types and functions.
"""

from __future__ import annotations

from zeroth.service.api.webhook_api import CreateSubscriptionRequest as CreateSubscriptionRequest
from zeroth.service.api.webhook_api import (
    WebhookDeadLetterListResponse as WebhookDeadLetterListResponse,
)
from zeroth.service.api.webhook_api import WebhookDeadLetterResponse as WebhookDeadLetterResponse
from zeroth.service.api.webhook_api import (
    WebhookSubscriptionListResponse as WebhookSubscriptionListResponse,
)
from zeroth.service.api.webhook_api import (
    WebhookSubscriptionResponse as WebhookSubscriptionResponse,
)
from zeroth.service.api.webhook_api import _mask_secret as _mask_secret
from zeroth.service.api.webhook_api import _serialize_subscription as _serialize_subscription
from zeroth.service.api.webhook_api import _webhook_service as _webhook_service
from zeroth.service.api.webhook_api import register_webhook_routes as register_webhook_routes
