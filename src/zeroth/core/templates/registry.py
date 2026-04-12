"""Versioned template registry for prompt templates.

This module provides an in-memory registry for storing and retrieving
versioned prompt templates. Templates are validated for correct Jinja2
syntax at registration time and variables are auto-extracted from the
template source.
"""

from __future__ import annotations

from datetime import UTC, datetime

import jinja2.meta
from jinja2.sandbox import SandboxedEnvironment

from zeroth.core.templates.errors import (
    TemplateNotFoundError,
    TemplateSyntaxValidationError,
    TemplateVersionExistsError,
)
from zeroth.core.templates.models import PromptTemplate


class TemplateRegistry:
    """In-memory registry for versioned prompt templates.

    Stores templates keyed by ``(name, version)`` and supports lookups
    by exact version, latest version, or listing all templates.
    Template syntax is validated at registration time using a
    ``SandboxedEnvironment``.
    """

    def __init__(self) -> None:
        self._templates: dict[str, dict[int, PromptTemplate]] = {}
        self._env = SandboxedEnvironment()

    def register(
        self,
        name: str,
        version: int,
        template_str: str,
        *,
        variables: list[str] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> PromptTemplate:
        """Register a new template version.

        Validates Jinja2 syntax and auto-extracts variables if not provided.
        Raises TemplateSyntaxValidationError for bad syntax and
        TemplateVersionExistsError for duplicate name+version.
        """
        # Validate syntax and extract AST
        try:
            ast = self._env.parse(template_str)
        except jinja2.TemplateSyntaxError as exc:
            msg = f"Template {name!r} version {version} has invalid syntax: {exc}"
            raise TemplateSyntaxValidationError(msg) from exc

        # Auto-extract variables if not explicitly provided
        if variables is None:
            variables = sorted(jinja2.meta.find_undeclared_variables(ast))

        # Check for duplicate
        if name in self._templates and version in self._templates[name]:
            msg = f"Template {name!r} version {version} already exists"
            raise TemplateVersionExistsError(msg)

        template = PromptTemplate(
            name=name,
            version=version,
            template_str=template_str,
            variables=variables,
            metadata=dict(metadata or {}),
            created_at=datetime.now(UTC),
        )

        if name not in self._templates:
            self._templates[name] = {}
        self._templates[name][version] = template

        return template

    def get(self, name: str, version: int | None = None) -> PromptTemplate:
        """Retrieve a template by name and optional version.

        If version is None, returns the latest version. Raises
        TemplateNotFoundError if the template or version does not exist.
        """
        if version is None:
            return self.get_latest(name)

        versions = self._templates.get(name)
        if versions is None or version not in versions:
            msg = f"Template {name!r} version {version} not found"
            raise TemplateNotFoundError(msg)

        return versions[version]

    def get_latest(self, name: str) -> PromptTemplate:
        """Retrieve the latest (highest version) template by name.

        Raises TemplateNotFoundError if the name is not registered.
        """
        versions = self._templates.get(name)
        if not versions:
            msg = f"Template {name!r} not found"
            raise TemplateNotFoundError(msg)

        latest_version = max(versions)
        return versions[latest_version]

    def list(self) -> list[PromptTemplate]:
        """Return all templates sorted by (name, version)."""
        result: list[PromptTemplate] = []
        for _name in sorted(self._templates):
            for _version in sorted(self._templates[_name]):
                result.append(self._templates[_name][_version])
        return result
