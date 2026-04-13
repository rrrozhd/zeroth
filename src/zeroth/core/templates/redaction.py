"""Secret variable redaction for rendered prompt templates.

Identifies template variables whose names match secret patterns
and redacts their values from the rendered output before it
reaches audit records.
"""

from __future__ import annotations

DEFAULT_SECRET_PATTERNS: tuple[str, ...] = (
    "secret",
    "token",
    "key",
    "password",
    "api_key",
)


def identify_secret_variables(
    variable_names: list[str],
    secret_patterns: tuple[str, ...] = DEFAULT_SECRET_PATTERNS,
) -> set[str]:
    """Identify which template variables are secrets based on name patterns.

    Scans each variable name (case-insensitive) for substring matches
    against the configured secret patterns. Returns the set of variable
    names that matched at least one pattern.
    """
    secrets: set[str] = set()
    for var in variable_names:
        var_lower = var.lower()
        for pattern in secret_patterns:
            if pattern in var_lower:
                secrets.add(var)
                break
    return secrets


def redact_rendered_prompt(
    rendered: str,
    variables: dict[str, object],
    secret_variable_names: set[str],
) -> str:
    """Replace secret variable values in the rendered prompt with redaction markers.

    Iterates over secret variable names in sorted order (for deterministic
    output) and replaces all occurrences of each secret value in the
    rendered string with ``***REDACTED***``.
    """
    redacted = rendered
    for var_name in sorted(secret_variable_names):
        value = variables.get(var_name)
        if value is not None:
            redacted = redacted.replace(str(value), "***REDACTED***")
    return redacted
