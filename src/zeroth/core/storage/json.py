"""JSON helpers for serializing Pydantic models into SQLite-safe payloads.

These functions convert Python objects (especially Pydantic models) to and
from JSON strings, so they can be stored in SQLite text columns and loaded
back out again.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, TypeAdapter


def to_json_value(value: BaseModel | dict[str, Any] | list[Any] | None) -> str:
    """Turn a Pydantic model, dict, or list into a compact JSON string.

    The output uses sorted keys and no extra whitespace, so it's
    consistent and easy to store in a database column.
    """
    if isinstance(value, BaseModel):
        payload: Any = value.model_dump(mode="json")
    else:
        payload = value
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def from_json_value(raw: str | bytes | None) -> Any:
    """Parse a JSON string (or bytes) back into a Python object.

    Returns None if the input is None.
    """
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def load_model[ModelT: BaseModel](
    raw: str | bytes | None,
    model_type: type[ModelT],
) -> ModelT | None:
    """Parse a JSON string directly into a specific Pydantic model.

    Returns None if the input is None. This is handy when you know
    exactly which model type you want to get back.
    """
    payload = from_json_value(raw)
    if payload is None:
        return None
    return model_type.model_validate(payload)


def load_typed_value(raw: str | bytes | None, annotation: Any) -> Any:
    """Parse a JSON string into any Python type described by a type annotation.

    Uses Pydantic's TypeAdapter to handle complex types like
    ``list[str]`` or ``dict[str, int]``.  Returns None if the input is None.
    """
    payload = from_json_value(raw)
    if payload is None:
        return None
    return TypeAdapter(annotation).validate_python(payload)
