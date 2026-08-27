from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import tool

from zeroth.check.adapter.bindings import BindingError, CheckBindings


def lookup(query: str, limit: int = 5) -> list[str]:
    return [query] * limit


class Repository:
    pass


def test_registers_callable_with_explicit_side_effect_and_stable_schema() -> None:
    bindings = CheckBindings(action_repository=Repository())
    returned = bindings.tool("lookup", lookup, "read_only")
    assert returned is lookup
    registered = bindings.registrations["lookup"]
    assert registered.side_effect == "read_only"
    assert registered.input_schema["required"] == ["query"]
    assert registered.input_schema_digest.startswith("sha256:")


def test_rejects_missing_or_invalid_classification_duplicate_and_blank_name() -> None:
    bindings = CheckBindings(action_repository=Repository())
    with pytest.raises(TypeError):
        bindings.tool("lookup", lookup)  # type: ignore[call-arg]
    with pytest.raises(BindingError):
        bindings.tool("lookup", lookup, "unknown")  # type: ignore[arg-type]
    with pytest.raises(BindingError):
        bindings.tool(" ", lookup, "read_only")
    bindings.tool("lookup", lookup, "read_only")
    with pytest.raises(BindingError):
        bindings.tool("lookup", lookup, "read_only")


def test_registry_freezes_and_repository_access_is_observed() -> None:
    repository = Repository()
    bindings = CheckBindings(action_repository=repository)
    assert bindings.action_repository is repository
    assert bindings.action_repository_requested is True
    bindings.freeze()
    with pytest.raises(BindingError, match="frozen"):
        bindings.tool("lookup", lookup, "read_only")


def test_replay_binds_replacement_without_retaining_live_callable() -> None:
    def taped(**arguments: Any) -> object:
        return arguments

    bindings = CheckBindings(
        action_repository=Repository(), mode="replay", replacements={"lookup": taped}
    )
    returned = bindings.tool("lookup", lookup, "read_only")
    assert returned is taped
    assert bindings.registrations["lookup"].implementation is None


def test_uninspectable_callable_is_rejected() -> None:
    bindings = CheckBindings(action_repository=Repository())
    with pytest.raises(BindingError, match="schema"):
        bindings.tool("bad", object(), "read_only")  # type: ignore[arg-type]


def test_registers_langchain_tool_schema() -> None:
    @tool
    def search(query: str) -> str:
        """Search a fixture corpus."""

        return query

    bindings = CheckBindings(action_repository=Repository())
    assert bindings.tool("search", search, "read_only") is search
    assert bindings.registrations["search"].input_schema["required"] == ["query"]
