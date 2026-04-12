"""Prompt template management for the Zeroth platform.

Provides versioned template registry, Pydantic models, and error hierarchy
for managing and rendering prompt templates.
"""

from __future__ import annotations

from zeroth.core.templates.errors import (
    TemplateError,
    TemplateNotFoundError,
    TemplateRenderError,
    TemplateSyntaxValidationError,
    TemplateVersionExistsError,
)
from zeroth.core.templates.models import (
    PromptTemplate,
    TemplateReference,
    TemplateRenderResult,
)
from zeroth.core.templates.registry import TemplateRegistry
from zeroth.core.templates.renderer import TemplateRenderer

__all__ = [
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
]
