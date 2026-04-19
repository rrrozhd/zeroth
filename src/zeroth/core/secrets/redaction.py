"""Helpers for redacting concrete secret values from payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class SecretRedactor:
    """Replace known secret values with stable redaction markers."""

    def __init__(self, known_secrets: Mapping[str, str] | None = None) -> None:
        self._known_secrets = {
            ref: value for ref, value in dict(known_secrets or {}).items() if value
        }

    def redact(self, value: Any) -> Any:
        """Recursively redact strings, dicts, and lists that contain known secrets."""
        if isinstance(value, str):
            redacted = value
            for ref, secret in self._known_secrets.items():
                redacted = redacted.replace(secret, f"[REDACTED:{ref}]")
            return redacted
        if isinstance(value, Mapping):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        return value


__all__ = ["SecretRedactor"]
