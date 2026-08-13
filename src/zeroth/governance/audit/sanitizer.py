"""Payload sanitization for audit records.

Provides the PayloadSanitizer class that removes or masks sensitive data
from audit payloads before they are stored, so secrets like passwords
and API keys don't end up in your audit logs.
"""

from __future__ import annotations

from typing import Any

from zeroth.governance.audit.capture_scrub import RedactionChain
from zeroth.governance.audit.models import AuditRedactionConfig


class PayloadSanitizer:
    """Cleans audit payloads by redacting or removing sensitive data.

    Given a redaction config, this class walks through a payload (dicts, lists,
    etc.) and replaces sensitive keys with "***REDACTED***" or drops entire
    paths that should not appear in audit logs.
    """

    def __init__(self, config: AuditRedactionConfig | None = None) -> None:
        self._chain = RedactionChain(redaction=config)

    def sanitize(self, payload: Any) -> Any:
        """Clean a payload by applying all configured redaction rules.

        Pass in any data structure (dict, list, or primitive) and get back
        a copy with sensitive values masked or removed.
        """
        return self._chain.scrub(payload)
