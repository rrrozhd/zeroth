"""Content-safety guardrails: detect / redact / block sensitive or disallowed content.

Unlike the model-boundary sanitizer (``agent_runtime.sanitization``, which guards
*untrusted tool/memory output*), these guardrails apply a content *policy* to an
agent's own typed input and output: PII detection/redaction and blocklist /
moderation filtering. They are opt-in (``AgentConfig.content_safety.enabled``)
because redacting or blocking an application's own data is high-blast-radius and
application-specific — the opposite trade-off from the secure-by-default sanitizer.

Pluggable: supply any object implementing ``ContentFilter`` (e.g. a moderation-API
client) to ``ContentGuardrail`` alongside or instead of the built-ins.

Note: the phone/credit-card heuristics are conservative and false-positive-prone;
they are safe under the default ``flag`` mode (audit only). Redaction and blocking
are always opt-in.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

_EMAIL = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"\b(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CREDIT_CARD = re.compile(r"\b(?:\d[ -]?){13,16}\b")


def _luhn_ok(value: str) -> bool:
    """Validate a candidate card number with the Luhn checksum (cuts false positives)."""
    digits = [int(c) for c in value if c.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# Applied in this order; each redacts its matches before the next runs, so the
# stricter numeric patterns (SSN, card) consume digits before the looser phone one.
_PII_SPECS: tuple[tuple[str, re.Pattern[str], object], ...] = (
    ("email", _EMAIL, None),
    ("ssn", _SSN, None),
    ("credit_card", _CREDIT_CARD, _luhn_ok),
    ("phone", _PHONE, None),
)


@dataclass(frozen=True)
class ContentFinding:
    """One category of sensitive/disallowed content found in a piece of text."""

    category: str
    count: int


class ContentFilter(Protocol):
    """A pluggable content filter applied to a single string."""

    def apply(self, text: str) -> tuple[str, tuple[ContentFinding, ...]]:
        """Return ``(redacted_text, findings)``.

        The redacted text is only used when the guardrail runs in ``redact`` mode;
        in ``flag``/``block`` mode only the findings matter.
        """
        ...


class PIIFilter:
    """Detects (and can redact) common PII: email, SSN, credit card, phone."""

    def __init__(self, types: tuple[str, ...] = ()) -> None:
        self._specs = tuple(spec for spec in _PII_SPECS if not types or spec[0] in types)

    def apply(self, text: str) -> tuple[str, tuple[ContentFinding, ...]]:
        findings: list[ContentFinding] = []
        out = text
        for name, pattern, validator in self._specs:
            count = 0

            def _repl(
                match: re.Match[str], _validator: object = validator, _name: str = name
            ) -> str:
                nonlocal count
                if callable(_validator) and not _validator(match.group(0)):
                    return match.group(0)
                count += 1
                return f"[REDACTED:{_name}]"

            out = pattern.sub(_repl, out)
            if count:
                findings.append(ContentFinding(category=f"pii:{name}", count=count))
        return out, tuple(findings)


class BlocklistFilter:
    """Detects (and can redact) configured terms, case-insensitively (literal match)."""

    def __init__(self, terms: tuple[str, ...] = ()) -> None:
        self._patterns = [re.compile(re.escape(term), re.IGNORECASE) for term in terms if term]

    def apply(self, text: str) -> tuple[str, tuple[ContentFinding, ...]]:
        out = text
        total = 0
        for pattern in self._patterns:
            count = 0

            def _repl(_match: re.Match[str]) -> str:
                nonlocal count
                count += 1
                return "[REDACTED:blocked]"

            out = pattern.sub(_repl, out)
            total += count
        findings = (ContentFinding("blocklist", total),) if total else ()
        return out, findings


@dataclass(frozen=True)
class GuardrailOutcome:
    """The result of inspecting one payload through a content guardrail."""

    payload: dict
    findings: tuple[ContentFinding, ...]
    direction: str
    mode: str
    blocked: bool

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    def as_audit(self) -> dict[str, object]:
        """JSON-friendly summary for the audit record."""
        return {
            "direction": self.direction,
            "mode": self.mode,
            "blocked": self.blocked,
            "findings": [{"category": f.category, "count": f.count} for f in self.findings],
        }


class ContentGuardrail:
    """Applies a set of ``ContentFilter`` s to an agent payload (input or output).

    Modes: ``flag`` records findings only; ``redact`` returns a redacted copy of
    the payload; ``block`` marks the outcome blocked when any finding is present
    (the caller raises). Walks string leaves of nested dicts/lists.
    """

    def __init__(
        self, *, filters: tuple[ContentFilter, ...] | list[ContentFilter], mode: str = "flag"
    ) -> None:
        if mode not in ("flag", "redact", "block"):
            raise ValueError("mode must be 'flag', 'redact', or 'block'")
        self._filters = tuple(filters)
        self._mode = mode

    def inspect(self, payload: Mapping[str, object], *, direction: str) -> GuardrailOutcome:
        totals: dict[str, int] = {}

        def _process(value: object) -> object:
            if isinstance(value, str):
                redacted = value
                for content_filter in self._filters:
                    redacted, found = content_filter.apply(redacted)
                    for finding in found:
                        totals[finding.category] = totals.get(finding.category, 0) + finding.count
                return redacted if self._mode == "redact" else value
            if isinstance(value, Mapping):
                return {key: _process(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [_process(item) for item in value]
            return value

        new_payload = _process(dict(payload))
        findings = tuple(
            ContentFinding(category, count) for category, count in sorted(totals.items())
        )
        blocked = self._mode == "block" and bool(findings)
        return GuardrailOutcome(
            payload=new_payload,  # type: ignore[arg-type]
            findings=findings,
            direction=direction,
            mode=self._mode,
            blocked=blocked,
        )
