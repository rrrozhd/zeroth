"""Prompt template contracts.

Provides versioned template registry, Pydantic models, and error hierarchy
for managing and rendering prompt templates.
"""

from __future__ import annotations

from zeroth.contracts.templates.errors import (
    TemplateError,
    TemplateNotFoundError,
    TemplateRenderError,
    TemplateSyntaxValidationError,
    TemplateVersionExistsError,
)
from zeroth.contracts.templates.models import (
    PromptTemplate,
    TemplateReference,
    TemplateRenderResult,
)
from zeroth.contracts.templates.redaction import (
    DEFAULT_SECRET_PATTERNS,
    identify_secret_variables,
    redact_rendered_prompt,
)
from zeroth.contracts.templates.registry import TemplateRegistry
from zeroth.contracts.templates.renderer import TemplateRenderer

__all__ = [
    "DEFAULT_SECRET_PATTERNS",
    "PromptTemplate",
    "TemplateError",
    "TemplateNotFoundError",
    "TemplateReference",
    "TemplateRegistry",
    "TemplateRenderError",
    "TemplateRenderer",
    "TemplateRenderResult",
    "TemplateSyntaxValidationError",
    "TemplateVersionExistsError",
    "identify_secret_variables",
    "redact_rendered_prompt",
]
