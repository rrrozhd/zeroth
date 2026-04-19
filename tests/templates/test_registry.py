from __future__ import annotations

import pytest

from zeroth.core.templates.errors import (
    TemplateNotFoundError,
    TemplateSyntaxValidationError,
    TemplateVersionExistsError,
)
from zeroth.core.templates.registry import TemplateRegistry


class TestRegistryRegister:
    def test_register_returns_prompt_template(self):
        registry = TemplateRegistry()
        tpl = registry.register(
            name="greeting",
            version=1,
            template_str="Hello {{ name }}!",
        )
        assert tpl.name == "greeting"
        assert tpl.version == 1
        assert tpl.template_str == "Hello {{ name }}!"
        assert tpl.created_at is not None

    def test_register_auto_extracts_variables(self):
        registry = TemplateRegistry()
        tpl = registry.register(
            name="msg",
            version=1,
            template_str="Dear {{ recipient }}, from {{ sender }}",
        )
        assert sorted(tpl.variables) == ["recipient", "sender"]

    def test_register_explicit_variables(self):
        registry = TemplateRegistry()
        tpl = registry.register(
            name="msg",
            version=1,
            template_str="Hello {{ name }}",
            variables=["name", "extra"],
        )
        assert tpl.variables == ["name", "extra"]

    def test_register_validates_syntax(self):
        registry = TemplateRegistry()
        with pytest.raises(TemplateSyntaxValidationError):
            registry.register(
                name="bad",
                version=1,
                template_str="Hello {{ name",
            )

    def test_register_duplicate_raises(self):
        registry = TemplateRegistry()
        registry.register(name="t", version=1, template_str="ok")
        with pytest.raises(TemplateVersionExistsError):
            registry.register(name="t", version=1, template_str="ok")


class TestRegistryGet:
    def test_get_exact_version(self):
        registry = TemplateRegistry()
        registry.register(name="t", version=1, template_str="v1")
        registry.register(name="t", version=2, template_str="v2")
        tpl = registry.get("t", version=1)
        assert tpl.version == 1
        assert tpl.template_str == "v1"

    def test_get_no_version_returns_latest(self):
        registry = TemplateRegistry()
        registry.register(name="t", version=1, template_str="v1")
        registry.register(name="t", version=3, template_str="v3")
        registry.register(name="t", version=2, template_str="v2")
        tpl = registry.get("t")
        assert tpl.version == 3
        assert tpl.template_str == "v3"

    def test_get_nonexistent_raises(self):
        registry = TemplateRegistry()
        with pytest.raises(TemplateNotFoundError):
            registry.get("nope", version=1)

    def test_get_nonexistent_version_raises(self):
        registry = TemplateRegistry()
        registry.register(name="t", version=1, template_str="ok")
        with pytest.raises(TemplateNotFoundError):
            registry.get("t", version=99)


class TestRegistryGetLatest:
    def test_get_latest_returns_highest(self):
        registry = TemplateRegistry()
        registry.register(name="t", version=1, template_str="v1")
        registry.register(name="t", version=5, template_str="v5")
        registry.register(name="t", version=3, template_str="v3")
        tpl = registry.get_latest("t")
        assert tpl.version == 5

    def test_get_latest_nonexistent_raises(self):
        registry = TemplateRegistry()
        with pytest.raises(TemplateNotFoundError):
            registry.get_latest("nope")


class TestRegistryList:
    def test_list_all_sorted(self):
        registry = TemplateRegistry()
        registry.register(name="b", version=2, template_str="b2")
        registry.register(name="a", version=1, template_str="a1")
        registry.register(name="b", version=1, template_str="b1")
        registry.register(name="a", version=2, template_str="a2")
        result = registry.list()
        names_versions = [(t.name, t.version) for t in result]
        assert names_versions == [("a", 1), ("a", 2), ("b", 1), ("b", 2)]

    def test_list_empty(self):
        registry = TemplateRegistry()
        assert registry.list() == []

    def test_multiple_versions_get_latest(self):
        registry = TemplateRegistry()
        registry.register(name="t", version=1, template_str="v1")
        registry.register(name="t", version=2, template_str="v2")
        registry.register(name="t", version=3, template_str="v3")
        latest = registry.get_latest("t")
        assert latest.version == 3
        assert latest.template_str == "v3"


class TestRegistryDelete:
    def test_delete_removes_version(self):
        registry = TemplateRegistry()
        registry.register(name="t", version=1, template_str="v1")
        registry.delete("t", 1)
        with pytest.raises(TemplateNotFoundError):
            registry.get("t", 1)

    def test_delete_preserves_other_versions(self):
        registry = TemplateRegistry()
        registry.register(name="t", version=1, template_str="v1")
        registry.register(name="t", version=2, template_str="v2")
        registry.delete("t", 1)
        tpl = registry.get("t", 2)
        assert tpl.version == 2
        assert tpl.template_str == "v2"

    def test_delete_nonexistent_name_raises(self):
        registry = TemplateRegistry()
        with pytest.raises(TemplateNotFoundError):
            registry.delete("nope", 1)

    def test_delete_nonexistent_version_raises(self):
        registry = TemplateRegistry()
        registry.register(name="t", version=1, template_str="v1")
        with pytest.raises(TemplateNotFoundError):
            registry.delete("t", 99)

    def test_delete_last_version_cleans_name(self):
        registry = TemplateRegistry()
        registry.register(name="t", version=1, template_str="v1")
        registry.delete("t", 1)
        assert registry.list() == []
