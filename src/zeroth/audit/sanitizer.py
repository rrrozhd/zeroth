"""Payload sanitization for audit records.

Provides the PayloadSanitizer class that removes or masks sensitive data
from audit payloads before they are stored, so secrets like passwords
and API keys don't end up in your audit logs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zeroth.audit.models import AuditRedactionConfig


class PayloadSanitizer:
    """Cleans audit payloads by redacting or removing sensitive data.

    Given a redaction config, this class walks through a payload (dicts, lists,
    etc.) and replaces sensitive keys with "***REDACTED***" or drops entire
    paths that should not appear in audit logs.
    """

    def __init__(self, config: AuditRedactionConfig | None = None) -> None:
        self._config = config or AuditRedactionConfig()

    def sanitize(self, payload: Any) -> Any:
        """Clean a payload by applying all configured redaction rules.

        Pass in any data structure (dict, list, or primitive) and get back
        a copy with sensitive values masked or removed.
        """
        return self._sanitize(payload, path=())

    def _sanitize(self, payload: Any, *, path: tuple[str, ...]) -> Any:
        """Recursively walk the payload, redacting or omitting as configured."""
        if path in self._config.omit_paths:
            return _Omitted
        if isinstance(payload, Mapping):
            result: dict[str, Any] = {}
            for key, value in payload.items():
                key_str = str(key)
                child_path = (*path, key_str)
                if child_path in self._config.omit_paths:
                    continue
                if key_str in self._config.redact_keys:
                    result[key_str] = "***REDACTED***"
                    continue
                sanitized = self._sanitize(value, path=child_path)
                if sanitized is not _Omitted:
                    result[key_str] = sanitized
            return result
        if isinstance(payload, list):
            return [
                item
                for value in payload
                if (item := self._sanitize(value, path=path)) is not _Omitted
            ]
        return payload


class _OmittedType:
    """Sentinel class used internally to mark values that should be dropped."""

    pass


_Omitted = _OmittedType()
