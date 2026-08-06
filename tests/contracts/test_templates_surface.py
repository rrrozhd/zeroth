"""Canonical import surface for the template contracts package.

Non-golden boundary tests for the Task 12 templates move: the canonical
``zeroth.contracts.templates`` package must publish the same objects the
legacy ``zeroth.core.templates`` path keeps republishing, and both packages
must stay cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys


def test_templates_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.contracts.templates as canonical

    expected = {
        "DEFAULT_SECRET_PATTERNS",
        "PromptTemplate",
        "TemplateError",
        "TemplateNotFoundError",
        "TemplateReference",
        "TemplateRegistry",
        "TemplateRenderError",
        "TemplateRenderResult",
        "TemplateRenderer",
        "TemplateSyntaxValidationError",
        "TemplateVersionExistsError",
        "identify_secret_variables",
        "redact_rendered_prompt",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.contracts.templates no longer publishes: {missing}"


def test_template_submodules_publish_their_names() -> None:
    from zeroth.contracts.templates import errors as canonical_errors
    from zeroth.contracts.templates import models as canonical_models

    assert hasattr(canonical_errors, "TemplateError")
    assert hasattr(canonical_errors, "TemplateNotFoundError")
    assert hasattr(canonical_models, "PromptTemplate")
    assert hasattr(canonical_models, "TemplateReference")
    assert hasattr(canonical_models, "TemplateRenderResult")


def test_templates_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.contracts.templates"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
