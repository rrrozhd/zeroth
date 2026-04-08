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
    return {
        "type": "json_schema",
        "json_schema": {
            "name": output_model.__name__,
            "schema": schema,
            "strict": True,
        },
    }
