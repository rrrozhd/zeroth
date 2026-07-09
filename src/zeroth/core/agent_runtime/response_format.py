"""Build LLM response_format from Pydantic output models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def build_response_format(output_model: type[BaseModel]) -> dict[str, Any] | None:
    """Build OpenAI-style response_format from a Pydantic model.

    Returns None if the model is a bare BaseModel (no custom fields),
    since that means no structured output constraint was intended.
    """
    # Skip bare BaseModel -- it means "any output", not "structured output"
    if output_model is BaseModel or not output_model.model_fields:
        return None
    schema = output_model.model_json_schema()
    # OpenAI structured outputs require additionalProperties: false AND
    # that every property appear in `required` on each object node.
    _enforce_strict_object_rules(schema)
    # Free-form object fields (dict[str, Any] and friends) cannot be expressed
    # in strict mode at all — strict requires every object to enumerate its
    # properties. Ship those schemas non-strict instead of letting the
    # provider reject the request outright.
    strict = not _has_freeform_object(schema)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": output_model.__name__,
            "schema": schema,
            "strict": strict,
        },
    }


def _has_freeform_object(schema: dict[str, Any]) -> bool:
    """True if any object node lacks enumerated properties (dict[str, Any])."""
    if (schema.get("type") == "object" or "properties" in schema) and not schema.get(
        "properties"
    ):
        return True
    for key in ("properties", "$defs"):
        sub = schema.get(key)
        if isinstance(sub, dict) and any(
            isinstance(v, dict) and _has_freeform_object(v) for v in sub.values()
        ):
            return True
    for key in ("items", "anyOf", "oneOf", "allOf"):
        sub = schema.get(key)
        if isinstance(sub, dict) and _has_freeform_object(sub):
            return True
        if isinstance(sub, list) and any(
            isinstance(item, dict) and _has_freeform_object(item) for item in sub
        ):
            return True
    return False


def _enforce_strict_object_rules(schema: dict[str, Any]) -> None:
    """Recursively make every object node satisfy OpenAI strict mode.

    OpenAI's structured output API, when ``strict`` is enabled, requires
    every object-type schema node to (1) set ``additionalProperties: false``
    and (2) list *every* property in ``required`` -- even fields that have a
    Python/Pydantic default. Pydantic omits defaulted fields from
    ``required``, so without this the API rejects the schema with
    "'required' is required to be supplied and to be an array including
    every key in properties."
    """
    if schema.get("type") == "object" or "properties" in schema:
        schema["additionalProperties"] = False
        properties = schema.get("properties")
        if properties:
            schema["required"] = list(properties.keys())
            for prop in properties.values():
                _enforce_strict_object_rules(prop)
    if "$defs" in schema:
        for defn in schema["$defs"].values():
            _enforce_strict_object_rules(defn)
    for key in ("items", "anyOf", "oneOf", "allOf"):
        sub = schema.get(key)
        if isinstance(sub, dict):
            _enforce_strict_object_rules(sub)
        elif isinstance(sub, list):
            for item in sub:
                if isinstance(item, dict):
                    _enforce_strict_object_rules(item)
