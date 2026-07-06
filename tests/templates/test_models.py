from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zeroth.core.templates.models import (
    PromptTemplate,
    TemplateReference,
    TemplateRenderResult,
)


class TestPromptTemplate:
    def test_round_trip_all_fields(self):
        now = datetime.now(UTC)
        tpl = PromptTemplate(
            name="greeting",
            version=1,
            template_str="Hello {{ name }}!",
            variables=["name"],
            metadata={"author": "test"},
            created_at=now,
        )
        assert tpl.name == "greeting"
        assert tpl.version == 1
        assert tpl.template_str == "Hello {{ name }}!"
        assert tpl.variables == ["name"]
        assert tpl.metadata == {"author": "test"}
        assert tpl.created_at == now

    def test_rejects_unknown_fields(self):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            PromptTemplate(
                name="t",
                version=1,
                template_str="ok",
                created_at=datetime.now(UTC),
                unknown_field="bad",
            )

    def test_frozen_immutable(self):
        tpl = PromptTemplate(
            name="t",
            version=1,
            template_str="ok",
            created_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            tpl.name = "changed"

    def test_default_variables_empty(self):
        tpl = PromptTemplate(
            name="t",
            version=1,
            template_str="ok",
            created_at=datetime.now(UTC),
        )
        assert tpl.variables == []

    def test_default_metadata_empty(self):
        tpl = PromptTemplate(
            name="t",
            version=1,
            template_str="ok",
            created_at=datetime.now(UTC),
        )
        assert tpl.metadata == {}


class TestTemplateReference:
    def test_round_trip_with_version(self):
        ref = TemplateReference(name="greeting", version=2)
        assert ref.name == "greeting"
        assert ref.version == 2

    def test_round_trip_latest(self):
        ref = TemplateReference(name="greeting")
        assert ref.name == "greeting"
        assert ref.version is None

    def test_frozen(self):
        ref = TemplateReference(name="greeting", version=1)
        with pytest.raises(ValidationError):
            ref.name = "changed"


class TestTemplateRenderResult:
    def test_round_trip(self):
        result = TemplateRenderResult(
            rendered="Hello world!",
            template_name="greeting",
            template_version=1,
            variables_used={"name": "world"},
        )
        assert result.rendered == "Hello world!"
        assert result.template_name == "greeting"
        assert result.template_version == 1
        assert result.variables_used == {"name": "world"}

    def test_default_variables_used_empty(self):
        result = TemplateRenderResult(
            rendered="ok",
            template_name="t",
            template_version=1,
        )
        assert result.variables_used == {}
