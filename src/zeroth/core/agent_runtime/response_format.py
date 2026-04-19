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
    # OpenAI structured outputs require additionalProperties: false
    _add_additional_properties_false(schema)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": output_model.__name__,
            "schema": schema,
            "strict": True,
        },
    }


def _add_additional_properties_false(schema: dict[str, Any]) -> None:
    """Recursively add additionalProperties: false to all object schemas.

    OpenAI's structured output API requires this on every object-type
    schema node when strict mode is enabled.
    """
    if schema.get("type") == "object" or "properties" in schema:
        schema["additionalProperties"] = False
        for prop in schema.get("properties", {}).values():
            _add_additional_properties_false(prop)
    if "$defs" in schema:
        for defn in schema["$defs"].values():
            _add_additional_properties_false(defn)
    for key in ("items", "anyOf", "oneOf", "allOf"):
        sub = schema.get(key)
        if isinstance(sub, dict):
            _add_additional_properties_false(sub)
        elif isinstance(sub, list):
            for item in sub:
                if isinstance(item, dict):
                    _add_additional_properties_false(item)
