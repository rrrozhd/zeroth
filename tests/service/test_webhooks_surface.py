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


def test_webhooks_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import webhooks as legacy
    from zeroth.service import webhooks as canonical

    for name in EXPORTS:
        assert getattr(canonical, name) is getattr(legacy, name), name


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
def test_webhooks_modules_are_the_same_surface_through_both_paths(
    module_name: str, names: tuple[str, ...]
) -> None:
    import importlib

    legacy_module = importlib.import_module(f"zeroth.core.webhooks.{module_name}")
    canonical_module = importlib.import_module(f"zeroth.service.webhooks.{module_name}")

    for name in names:
        assert getattr(canonical_module, name) is getattr(legacy_module, name), name


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.service.webhooks", "zeroth.core.webhooks"),
        ("zeroth.core.webhooks", "zeroth.service.webhooks"),
    ],
)
def test_webhooks_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
