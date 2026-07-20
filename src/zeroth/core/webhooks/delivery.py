"""Legacy import path for :mod:`zeroth.service.webhooks.delivery`."""

from zeroth.service.webhooks.delivery import WebhookDeliveryWorker, next_retry_delay

__all__ = [
    "WebhookDeliveryWorker",
    "next_retry_delay",
]
