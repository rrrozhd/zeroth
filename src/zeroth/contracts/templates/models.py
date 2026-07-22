"""Pydantic models for the template subsystem.

Defines the core data shapes: PromptTemplate for stored templates,
TemplateReference for lightweight pointers, and TemplateRenderResult
for render output.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PromptTemplate(BaseModel):
    """A versioned prompt template with its source and metadata.

    Templates are immutable once registered. The ``variables`` field holds
    the names of template variables extracted from the Jinja2 source.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: int
    template_str: str
    variables: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TemplateReference(BaseModel):
    """A lightweight pointer to a template by name and optional version.

    When version is None, the latest version is assumed during resolution.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: int | None = None


class TemplateRenderResult(BaseModel):
    """The result of rendering a template with variables.

    Captures the rendered output alongside provenance information about
    which template and variables were used.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rendered: str
    template_name: str
    template_version: int
    variables_used: dict[str, Any] = Field(default_factory=dict)
