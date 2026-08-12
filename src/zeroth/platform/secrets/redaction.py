"""Helpers for redacting concrete secret values from payloads."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

SecretReference = str | tuple[str, str]
SecretSeeds = Mapping[SecretReference, str] | Iterable[tuple[SecretReference, str]]


def _marker(reference: SecretReference) -> str:
    """Return a stable marker without exposing tenant-qualified identities."""
    if isinstance(reference, tuple):
        return "SECRET"
    return reference.replace(".", "_").replace("-", "_").upper()


class SecretRedactor:
    """Replace known secret values with stable redaction markers."""

    def __init__(self, known_secrets: SecretSeeds | None = None) -> None:
        """Build from named mappings or a sequence of ``(reference, value)`` seeds.

        String references are normalized to the conventional uppercase marker;
        tenant-qualified tuple references always use the opaque ``SECRET`` marker.
        Equal values are intentionally reduced to one deterministic marker.
        """
        entries = known_secrets.items() if isinstance(known_secrets, Mapping) else known_secrets
        self._marker_by_value: dict[str, str] = {}
        for reference, secret in entries or ():
            if not secret:
                continue
            marker = _marker(reference)
            previous = self._marker_by_value.get(secret)
            if previous is None or marker < previous:
                self._marker_by_value[secret] = marker
        ordered_values = sorted(self._marker_by_value, key=lambda item: (-len(item), item))
        self._matcher = (
            re.compile("|".join(re.escape(secret) for secret in ordered_values))
            if ordered_values
            else None
        )

    def redact(self, value: Any) -> Any:
        """Recursively redact strings, dicts, and lists that contain known secrets."""
        if isinstance(value, str):
            if self._matcher is None:
                return value
            return self._matcher.sub(
                lambda match: f"[REDACTED:{self._marker_by_value[match.group(0)]}]",
                value,
            )
        if isinstance(value, Mapping):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        return value


__all__ = ["SecretRedactor"]
