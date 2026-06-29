"""Tests for build_response_format -- OpenAI strict structured-output schemas.

OpenAI's strict mode requires that every object node set
``additionalProperties: false`` and that ``required`` list *every* property,
including fields that carry a Pydantic default. These tests pin that
behaviour so a regression cannot silently re-break live structured output
(see examples/02_multi_agent.py, which uses a contract with defaulted fields).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from zeroth.core.agent_runtime.response_format import build_response_format


class _Nested(BaseModel):
    label: str
    weight: float = 1.0


class _WithDefaults(BaseModel):
    """Mirrors the shape of the Research contract that failed live."""

    topic: str
    findings: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    nested: _Nested | None = None


def _walk_objects(schema: dict[str, Any]):
    """Yield every object-type node in a JSON schema (incl. $defs)."""
    if schema.get("type") == "object" or "properties" in schema:
        yield schema
        for prop in schema.get("properties", {}).values():
            yield from _walk_objects(prop)
    for defn in schema.get("$defs", {}).values():
        yield from _walk_objects(defn)
    for key in ("items", "anyOf", "oneOf", "allOf"):
        sub = schema.get(key)
        if isinstance(sub, dict):
            yield from _walk_objects(sub)
        elif isinstance(sub, list):
            for item in sub:
                if isinstance(item, dict):
                    yield from _walk_objects(item)


def test_bare_basemodel_returns_none() -> None:
    assert build_response_format(BaseModel) is None


def test_envelope_shape_and_strict_flag() -> None:
    rf = build_response_format(_WithDefaults)
    assert rf is not None
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "_WithDefaults"
    assert rf["json_schema"]["strict"] is True


def test_defaulted_fields_are_required() -> None:
    """The original bug: defaulted fields were omitted from `required`."""
    rf = build_response_format(_WithDefaults)
    schema = rf["json_schema"]["schema"]
    assert set(schema["required"]) == {"topic", "findings", "sources", "nested"}


def test_every_object_node_is_openai_strict_valid() -> None:
    """Strict mode: each object node must require all of its properties
    and forbid additional properties -- including nested $defs."""
    rf = build_response_format(_WithDefaults)
    schema = rf["json_schema"]["schema"]
    nodes = list(_walk_objects(schema))
    # top-level + the nested model in $defs
    assert len(nodes) >= 2
    for node in nodes:
        props = node.get("properties")
        if not props:
            continue
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(props.keys())
