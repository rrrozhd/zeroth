"""Canonical import surface for the template contracts package.

Non-golden boundary tests for the Task 12 templates move: the canonical
``zeroth.contracts.templates`` package must publish the same objects the
legacy ``zeroth.core.templates`` path keeps republishing, and both packages
must stay cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_templates_is_the_same_surface_through_both_paths() -> None:
    from zeroth.contracts import templates as canonical
    from zeroth.core import templates as legacy

    assert canonical.DEFAULT_SECRET_PATTERNS is legacy.DEFAULT_SECRET_PATTERNS
    assert canonical.PromptTemplate is legacy.PromptTemplate
    assert canonical.TemplateError is legacy.TemplateError
    assert canonical.TemplateNotFoundError is legacy.TemplateNotFoundError
    assert canonical.TemplateReference is legacy.TemplateReference
    assert canonical.TemplateRegistry is legacy.TemplateRegistry
    assert canonical.TemplateRenderError is legacy.TemplateRenderError
    assert canonical.TemplateRenderer is legacy.TemplateRenderer
    assert canonical.TemplateRenderResult is legacy.TemplateRenderResult
    assert canonical.TemplateSyntaxValidationError is legacy.TemplateSyntaxValidationError
    assert canonical.TemplateVersionExistsError is legacy.TemplateVersionExistsError
    assert canonical.identify_secret_variables is legacy.identify_secret_variables
    assert canonical.redact_rendered_prompt is legacy.redact_rendered_prompt


def test_template_submodules_are_the_same_surface_through_both_paths() -> None:
    from zeroth.contracts.templates import errors as canonical_errors
    from zeroth.contracts.templates import models as canonical_models
    from zeroth.core.templates import errors as legacy_errors
    from zeroth.core.templates import models as legacy_models

    assert canonical_errors.TemplateError is legacy_errors.TemplateError
    assert canonical_errors.TemplateNotFoundError is legacy_errors.TemplateNotFoundError
    assert canonical_models.PromptTemplate is legacy_models.PromptTemplate
    assert canonical_models.TemplateReference is legacy_models.TemplateReference
    assert canonical_models.TemplateRenderResult is legacy_models.TemplateRenderResult


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.contracts.templates", "zeroth.core.templates"),
        ("zeroth.core.templates", "zeroth.contracts.templates"),
    ],
)
def test_templates_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
