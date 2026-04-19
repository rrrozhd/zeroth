"""Jinja2 sandboxed template renderer.

Provides safe template rendering using Jinja2's SandboxedEnvironment.
The sandbox blocks access to dangerous attributes (``__class__``,
``__bases__``, etc.) and StrictUndefined ensures missing variables
raise errors rather than silently rendering empty strings.
"""

from __future__ import annotations

import jinja2.meta
from jinja2 import StrictUndefined, TemplateSyntaxError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment, SecurityError

from zeroth.core.templates.errors import (
    TemplateRenderError,
    TemplateSyntaxValidationError,
)
from zeroth.core.templates.models import PromptTemplate, TemplateRenderResult


class TemplateRenderer:
    """Renders prompt templates safely using a Jinja2 SandboxedEnvironment.

    The sandbox prevents template injection attacks by blocking access to
    dunder attributes. StrictUndefined ensures that any missing variable
    raises an error rather than silently producing empty output.

    Autoescape is off (the default) because prompt templates produce plain
    text for LLM consumption, not HTML.
    """

    def __init__(self) -> None:
        # autoescape=False is the Jinja2 default. We leave it off because
        # prompts are plain text destined for LLM consumption, not HTML.
        self._env = SandboxedEnvironment(undefined=StrictUndefined)

    def render(
        self,
        template: PromptTemplate,
        variables: dict[str, object],
    ) -> TemplateRenderResult:
        """Render a template with the given variables.

        Raises TemplateRenderError if a variable is undefined or if the
        template attempts to access a sandboxed attribute.
        """
        try:
            jinja_template = self._env.from_string(template.template_str)
            rendered = jinja_template.render(variables)
        except UndefinedError as exc:
            msg = (
                f"Undefined variable in template {template.name!r} "
                f"version {template.version}: {exc}"
            )
            raise TemplateRenderError(msg) from exc
        except SecurityError as exc:
            msg = (
                f"Security violation in template {template.name!r} "
                f"version {template.version}: {exc}"
            )
            raise TemplateRenderError(msg) from exc

        return TemplateRenderResult(
            rendered=rendered,
            template_name=template.name,
            template_version=template.version,
            variables_used=dict(variables),
        )

    def validate_syntax(self, template_str: str) -> list[str]:
        """Validate template syntax and return sorted list of variable names.

        Raises TemplateSyntaxValidationError if the template has invalid
        Jinja2 syntax.
        """
        try:
            ast = self._env.parse(template_str)
        except TemplateSyntaxError as exc:
            msg = f"Invalid template syntax: {exc}"
            raise TemplateSyntaxValidationError(msg) from exc

        return sorted(jinja2.meta.find_undeclared_variables(ast))
