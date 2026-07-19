"""Legacy import path for the template contracts package.

Prompt templates live in :mod:`zeroth.contracts.templates`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.contracts.templates import (
    DEFAULT_SECRET_PATTERNS,
    PromptTemplate,
    TemplateError,
    TemplateNotFoundError,
    TemplateReference,
    TemplateRegistry,
    TemplateRenderer,
    TemplateRenderError,
    TemplateRenderResult,
    TemplateSyntaxValidationError,
    TemplateVersionExistsError,
    identify_secret_variables,
    redact_rendered_prompt,
)

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
