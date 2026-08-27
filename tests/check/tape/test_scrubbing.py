from __future__ import annotations

from zeroth.check.tape.scrubbing import SecretScanner, scrub_secrets


def test_detects_nested_secret_names_common_tokens_and_entropy() -> None:
    value = {
        "nested": {"api_key": "ordinary-looking-value"},
        "token": "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "opaque": "A8vD2qZ9mN4kR7wP6cX3tB5jH1sF0uL9",
    }
    findings = SecretScanner().scan(value)
    assert {finding.path for finding in findings} >= {
        "$.nested.api_key",
        "$.token",
        "$.opaque",
    }


def test_scrubbing_is_deterministic_and_equality_preserving() -> None:
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    value = {"first": secret, "nested": [secret]}
    first = scrub_secrets(value)
    second = scrub_secrets(value)
    assert first.value == second.value
    assert first.value["first"] == first.value["nested"][0]
    assert secret not in str(first.value)
    assert SecretScanner().scan(first.value) == ()


def test_allowlist_suppresses_known_false_positive() -> None:
    value = {"token": "documented-fixture-value"}
    assert SecretScanner(allowlist={"documented-fixture-value"}).scan(value) == ()
