"""Unit tests for content-safety guardrails (SAFE)."""

from __future__ import annotations

from zeroth.governance.guardrails.content import (
    BlocklistFilter,
    ContentGuardrail,
    PIIFilter,
)


def test_pii_filter_detects_and_redacts_email() -> None:
    out, findings = PIIFilter().apply("reach me at jane.doe@example.com today")
    assert "[REDACTED:email]" in out
    assert "jane.doe@example.com" not in out
    assert ("pii:email", 1) in [(f.category, f.count) for f in findings]


def test_pii_filter_detects_ssn_and_phone() -> None:
    _, findings = PIIFilter().apply("ssn 123-45-6789 call 555-123-4567")
    categories = {f.category for f in findings}
    assert "pii:ssn" in categories
    assert "pii:phone" in categories


def test_pii_filter_credit_card_requires_luhn() -> None:
    # 4111 1111 1111 1111 is Luhn-valid; ...1112 is not.
    _, valid = PIIFilter(types=("credit_card",)).apply("card 4111 1111 1111 1111")
    _, invalid = PIIFilter(types=("credit_card",)).apply("card 4111 1111 1111 1112")
    assert any(f.category == "pii:credit_card" for f in valid)
    assert not invalid


def test_pii_filter_types_restriction() -> None:
    _, findings = PIIFilter(types=("email",)).apply("a@b.com and 123-45-6789")
    categories = {f.category for f in findings}
    assert categories == {"pii:email"}  # ssn ignored


def test_blocklist_filter_is_case_insensitive_and_counts() -> None:
    out, findings = BlocklistFilter(terms=("forbidden",)).apply("This is Forbidden and forbidden.")
    assert findings[0].category == "blocklist"
    assert findings[0].count == 2
    assert "forbidden" not in out.lower()


def test_guardrail_flag_mode_records_findings_without_modifying() -> None:
    guard = ContentGuardrail(filters=[PIIFilter()], mode="flag")
    outcome = guard.inspect({"note": "mail a@b.com"}, direction="input")
    assert outcome.has_findings
    assert outcome.blocked is False
    assert outcome.payload == {"note": "mail a@b.com"}  # unchanged in flag mode


def test_guardrail_redact_mode_rewrites_payload() -> None:
    guard = ContentGuardrail(filters=[PIIFilter()], mode="redact")
    outcome = guard.inspect({"note": "mail a@b.com"}, direction="output")
    assert outcome.payload["note"] == "mail [REDACTED:email]"
    assert outcome.blocked is False


def test_guardrail_block_mode_flags_blocked_when_findings() -> None:
    guard = ContentGuardrail(filters=[PIIFilter()], mode="block")
    blocked = guard.inspect({"note": "a@b.com"}, direction="output")
    clean = guard.inspect({"note": "nothing here"}, direction="output")
    assert blocked.blocked is True
    assert clean.blocked is False
    assert clean.findings == ()


def test_guardrail_walks_nested_structures() -> None:
    guard = ContentGuardrail(filters=[PIIFilter()], mode="redact")
    outcome = guard.inspect(
        {"items": [{"email": "a@b.com"}, {"email": "c@d.com"}]}, direction="input"
    )
    emails = outcome.payload["items"]
    assert emails[0]["email"] == "[REDACTED:email]"
    assert emails[1]["email"] == "[REDACTED:email]"
    assert outcome.findings[0].category == "pii:email"
    assert outcome.findings[0].count == 2


def test_guardrail_as_audit_shape() -> None:
    guard = ContentGuardrail(filters=[PIIFilter()], mode="flag")
    audit = guard.inspect({"note": "a@b.com"}, direction="input").as_audit()
    assert audit["direction"] == "input"
    assert audit["mode"] == "flag"
    assert audit["blocked"] is False
    assert audit["findings"] == [{"category": "pii:email", "count": 1}]
