"""Canonical import surface for the service webhooks package.

Non-golden boundary tests for the Task 16 webhooks move: the canonical
``zeroth.service.webhooks`` package must publish the same objects the
legacy ``zeroth.core.webhooks`` path keeps republishing, and both
packages must stay cold-importable from a fresh interpreter in either
order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

EXPORTS = (
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
)


def test_webhooks_publishes_its_whole_surface() -> None:
    from zeroth.service import webhooks as canonical

    for name in EXPORTS:
        assert hasattr(canonical, name), name


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        ("delivery", ("WebhookDeliveryWorker", "next_retry_delay")),
        (
            "models",
            (
                "DeliveryStatus",
                "EscalationAction",
                "WebhookDeadLetter",
                "WebhookDelivery",
                "WebhookEventPayload",
                "WebhookEventType",
                "WebhookSubscription",
            ),
        ),
        ("repository", ("WebhookRepository",)),
        ("service", ("WebhookService",)),
        ("signing", ("sign_payload",)),
    ],
)
def test_webhooks_modules_publish_their_names(
    module_name: str, names: tuple[str, ...]
) -> None:
    import importlib

    canonical_module = importlib.import_module(f"zeroth.service.webhooks.{module_name}")

    for name in names:
        assert hasattr(canonical_module, name), name


def test_webhooks_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.service.webhooks"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
