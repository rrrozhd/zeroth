"""Tests for secret variable identification and prompt redaction.

Verifies that template variables whose names match secret patterns are
identified correctly, and that their values are replaced with redaction
markers in the rendered prompt output.
"""

from __future__ import annotations

import pytest

from zeroth.core.templates.redaction import (
    DEFAULT_SECRET_PATTERNS,
    identify_secret_variables,
    redact_rendered_prompt,
)


class TestDefaultSecretPatterns:
    def test_contains_required_patterns(self):
        required = {"secret", "token", "key", "password", "api_key"}
        assert required.issubset(set(DEFAULT_SECRET_PATTERNS))


class TestIdentifySecretVariables:
    def test_matches_key_pattern(self):
        result = identify_secret_variables(["name", "api_key", "role"])
        assert result == {"api_key"}

    def test_matches_token_and_password(self):
        result = identify_secret_variables(["user_token", "password_hash"])
        assert result == {"user_token", "password_hash"}

    def test_no_matches_returns_empty(self):
        result = identify_secret_variables(["name", "role"])
        assert result == set()

    def test_custom_patterns(self):
        result = identify_secret_variables(
            ["my_custom_var", "other"],
            secret_patterns=("custom",),
        )
        assert result == {"my_custom_var"}

    def test_case_insensitive_matching(self):
        result = identify_secret_variables(["API_KEY", "Secret_Value"])
        assert "API_KEY" in result
        assert "Secret_Value" in result

    def test_multiple_pattern_matches(self):
        """A variable matching multiple patterns is only included once."""
        result = identify_secret_variables(["secret_token"])
        assert result == {"secret_token"}


class TestRedactRenderedPrompt:
    def test_redacts_secret_values(self):
        rendered = "Hello! Your key is sk-abc123. Use it wisely."
        variables = {"name": "Alice", "api_key": "sk-abc123"}
        secret_names = {"api_key"}
        result = redact_rendered_prompt(rendered, variables, secret_names)
        assert "sk-abc123" not in result
        assert "***REDACTED***" in result
        assert "Hello!" in result

    def test_no_secrets_returns_unchanged(self):
        rendered = "Hello Alice!"
        variables = {"name": "Alice"}
        result = redact_rendered_prompt(rendered, variables, set())
        assert result == "Hello Alice!"

    def test_multiple_secret_values(self):
        rendered = "Key=sk-123 Token=tok-456"
        variables = {"api_key": "sk-123", "auth_token": "tok-456"}
        secret_names = {"api_key", "auth_token"}
        result = redact_rendered_prompt(rendered, variables, secret_names)
        assert "sk-123" not in result
        assert "tok-456" not in result
        assert result.count("***REDACTED***") == 2

    def test_missing_variable_skipped(self):
        """If a secret variable name is not in the variables dict, skip it."""
        rendered = "Hello world"
        variables = {"name": "world"}
        secret_names = {"api_key"}
        result = redact_rendered_prompt(rendered, variables, secret_names)
        assert result == "Hello world"

    def test_deterministic_output(self):
        """Redaction should be deterministic (sorted variable iteration)."""
        rendered = "a=x b=y"
        variables = {"secret_b": "y", "secret_a": "x"}
        secret_names = {"secret_a", "secret_b"}
        result1 = redact_rendered_prompt(rendered, variables, secret_names)
        result2 = redact_rendered_prompt(rendered, variables, secret_names)
        assert result1 == result2
