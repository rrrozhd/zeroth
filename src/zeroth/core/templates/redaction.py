"""Legacy import path for :mod:`zeroth.contracts.templates.redaction`."""

from zeroth.contracts.templates.redaction import (
    DEFAULT_SECRET_PATTERNS,
    identify_secret_variables,
    redact_rendered_prompt,
)

__all__ = [
    "DEFAULT_SECRET_PATTERNS",
    "identify_secret_variables",
    "redact_rendered_prompt",
]
