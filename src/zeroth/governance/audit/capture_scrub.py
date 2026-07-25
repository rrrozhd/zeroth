"""The capture boundary's redaction chain, applied to keys as well as values.

Split out of :mod:`zeroth.governance.audit.capture_policy` because it is the
one part of the boundary that is a *complement* rather than the guarantee: the
channel drop and the typed metadata allowlist are what hold, and this chain is
what tightens whatever those let through.

**The invariant: a mapping key is treated exactly like a value.** Every rung of
the chain used to walk values only. :class:`~zeroth.platform.secrets.redaction.SecretRedactor`
rebuilds a mapping as ``{key: redact(item)}``, and the PII walk did the same, so
a *registered* secret used as a mapping key was reproduced verbatim in a
content-mode record -- the one place the channel drop does not cover. Keys now go
through the same masking as values, and a key that is not an exact ``str``
becomes :data:`~zeroth.governance.audit.capture_projection.REDACTED` rather than
``str(key)``, which would have dispatched to the key's own ``__str__``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zeroth.governance.audit.capture_projection import MAX_DEPTH, REDACTED
from zeroth.governance.audit.models import AuditRedactionConfig
from zeroth.governance.audit.sanitizer import PayloadSanitizer
from zeroth.governance.guardrails.content import PIIFilter
from zeroth.platform.secrets.redaction import SecretRedactor

# Exact-match keys, because PayloadSanitizer compares key names literally. This
# set is a floor a caller may widen and cannot narrow; it is the complement to
# the channel drop, not the mechanism the guarantee rests on.
DEFAULT_REDACT_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "credentials",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)

# Only these patterns: PIIFilter's phone heuristic matches any ten-digit run.
_PII_PATTERNS: tuple[str, ...] = ("email", "ssn", "credit_card")


class RedactionChain:
    """Key redaction, registered-secret masking and PII filtering over one payload.

    Args:
        redaction: Extra key-redaction and path-omission rules, merged with
            :data:`DEFAULT_REDACT_KEYS` -- a supplied config widens the default
            and cannot narrow it.
        known_secrets: Resolved secret values to mask wherever they appear,
            including in mapping keys. Registering none only weakens the
            value-based complement.
    """

    def __init__(
        self,
        *,
        redaction: AuditRedactionConfig | None = None,
        known_secrets: Mapping[str, str] | None = None,
    ) -> None:
        config = AuditRedactionConfig() if redaction is None else redaction
        self._sanitizer = PayloadSanitizer(
            AuditRedactionConfig(
                redact_keys=set(DEFAULT_REDACT_KEYS) | set(config.redact_keys),
                omit_paths=set(config.omit_paths),
            )
        )
        self._secrets = SecretRedactor(known_secrets)
        self._pii = PIIFilter(_PII_PATTERNS)

    def scrub(self, value: Any) -> Any:
        """Apply key redaction, then registered-secret masking, then PII filtering."""
        return self._filter_pii(self._secrets.redact(self._sanitizer.sanitize(value)))

    def scrub_key(self, key: Any) -> str:
        """Mask one mapping key, accepting only an exact ``str`` to begin with."""
        if type(key) is not str:
            return REDACTED
        masked = self._secrets.redact(key)
        if type(masked) is not str:
            return REDACTED
        filtered, _findings = self._pii.apply(masked)
        return filtered

    def _filter_pii(self, value: Any, *, depth: int = 0) -> Any:
        """Walk string leaves and mapping keys through the PII filter.

        ``isinstance``, not the house exact-type check: here the widened branch
        filters *more*, and ``re.sub`` returns a plain ``str``, so no ``str``
        subclass survives into the persisted record.
        """
        if depth >= MAX_DEPTH:
            return REDACTED
        if isinstance(value, str):
            filtered, _findings = self._pii.apply(value)
            return filtered
        if isinstance(value, Mapping):
            return {
                self.scrub_key(key): self._filter_pii(item, depth=depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._filter_pii(item, depth=depth + 1) for item in value]
        return value


__all__ = ["DEFAULT_REDACT_KEYS", "RedactionChain"]
