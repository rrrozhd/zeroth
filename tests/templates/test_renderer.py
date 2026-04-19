from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zeroth.core.templates.errors import (
    TemplateRenderError,
    TemplateSyntaxValidationError,
)
from zeroth.core.templates.models import PromptTemplate, TemplateRenderResult
from zeroth.core.templates.renderer import TemplateRenderer


def _make_template(
    name: str = "test",
    version: int = 1,
    template_str: str = "Hello {{ name }}!",
    variables: list[str] | None = None,
) -> PromptTemplate:
    return PromptTemplate(
        name=name,
        version=version,
        template_str=template_str,
        variables=variables or [],
        created_at=datetime.now(UTC),
    )


class TestRendererRender:
    def test_render_valid_template(self):
        renderer = TemplateRenderer()
        tpl = _make_template(template_str="Hello {{ name }}!")
        result = renderer.render(tpl, {"name": "world"})
        assert result.rendered == "Hello world!"

    def test_render_with_jinja2_filters(self):
        renderer = TemplateRenderer()
        tpl = _make_template(template_str="Items: {{ items | join(', ') }}")
        result = renderer.render(tpl, {"items": ["a", "b", "c"]})
        assert result.rendered == "Items: a, b, c"

    def test_render_undefined_variable_raises(self):
        renderer = TemplateRenderer()
        tpl = _make_template(template_str="Hello {{ name }}!")
        with pytest.raises(TemplateRenderError, match="test.*version 1"):
            renderer.render(tpl, {})

    def test_render_injection_attack_raises(self):
        renderer = TemplateRenderer()
        tpl = _make_template(
            template_str="{{ [].__class__.__bases__ }}",
        )
        with pytest.raises(TemplateRenderError, match="test.*version 1"):
            renderer.render(tpl, {})

    def test_render_returns_template_render_result(self):
        renderer = TemplateRenderer()
        tpl = _make_template(
            name="greeting",
            version=3,
            template_str="Hi {{ who }}",
        )
        result = renderer.render(tpl, {"who": "there"})
        assert isinstance(result, TemplateRenderResult)
        assert result.rendered == "Hi there"
        assert result.template_name == "greeting"
        assert result.template_version == 3
        assert result.variables_used == {"who": "there"}

    def test_render_merges_multiple_variable_sources(self):
        renderer = TemplateRenderer()
        tpl = _make_template(
            template_str="{{ input_val }} {{ state_val }} {{ memory_val }}",
        )
        variables = {
            "input_val": "from_input",
            "state_val": "from_state",
            "memory_val": "from_memory",
        }
        result = renderer.render(tpl, variables)
        assert result.rendered == "from_input from_state from_memory"


class TestRendererValidateSyntax:
    def test_valid_syntax_returns_variables(self):
        renderer = TemplateRenderer()
        variables = renderer.validate_syntax("Hello {{ name }} and {{ age }}")
        assert variables == ["age", "name"]

    def test_invalid_syntax_raises(self):
        renderer = TemplateRenderer()
        with pytest.raises(TemplateSyntaxValidationError):
            renderer.validate_syntax("Hello {{ name")


class TestRendererNoAutoescape:
    def test_no_autoescape(self):
        """Prompts are plain text, not HTML. Autoescape must be off."""
        renderer = TemplateRenderer()
        tpl = _make_template(template_str="{{ html_content }}")
        result = renderer.render(tpl, {"html_content": "<b>bold</b>"})
        # If autoescape were on, this would be &lt;b&gt;bold&lt;/b&gt;
        assert result.rendered == "<b>bold</b>"
