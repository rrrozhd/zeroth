"""Error types for the template subsystem.

These exceptions are raised when something goes wrong while registering,
looking up, rendering, or validating templates. They all inherit from
TemplateError, so you can catch that one base class to handle any
template-related error.
"""

from __future__ import annotations


class TemplateError(Exception):
    """Base error for anything that goes wrong in the template subsystem.

    Catch this if you want to handle all template-related errors in one place.
    More specific errors below inherit from this one.
    """


class TemplateNotFoundError(TemplateError):
    """Raised when a requested template or version is missing.

    This is raised when a lookup by template name and version cannot find a
    matching registry entry.
    """


class TemplateVersionExistsError(TemplateError):
    """Raised when a template version is registered twice.

    Each ``(name, version)`` pair must be unique within the registry.
    """


class TemplateRenderError(TemplateError):
    """Raised when a template fails to render.

    This covers undefined variable errors (StrictUndefined) and security
    violations caught by the Jinja2 SandboxedEnvironment.
    """


class TemplateSyntaxValidationError(TemplateError):
    """Raised when a template contains invalid Jinja2 syntax.

    This is detected at registration time before the template is stored.
    """
