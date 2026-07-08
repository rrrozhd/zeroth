"""Runtime models for schema-only contracts (authored as raw JSON Schema).

Console-authored contracts have no Python class behind them — just a JSON
Schema. This module synthesizes a Pydantic model for such a contract so every
existing call site (`resolve_model_type` consumers: run ingress validation,
the agent-runner factory) keeps working unchanged:

* validation is EXACT: a ``jsonschema`` validator runs over the raw payload in
  a before-validator, so arbitrary schema keywords (pattern, oneOf, bounds…)
  are honored — not just the subset Pydantic fields could express;
* ``model_json_schema()`` returns the authored schema verbatim, so structured
  generation and OpenAPI surfaces see exactly what the user wrote;
* ``extra="allow"`` keeps every payload field, so ``model_dump()`` round-trips
  the full validated payload.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from jsonschema import validators
from pydantic import BaseModel, ConfigDict, model_validator


def check_json_schema(schema: dict[str, Any]) -> None:
    """Raise ``jsonschema.SchemaError`` if the schema itself is malformed."""
    validator_cls = validators.validator_for(schema)
    validator_cls.check_schema(schema)


def model_from_json_schema(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """Build a Pydantic model that validates payloads against ``schema``."""
    check_json_schema(schema)
    validator_cls = validators.validator_for(schema)
    checker = validator_cls(schema)
    frozen_schema = copy.deepcopy(schema)

    class SchemaContractModel(BaseModel):
        model_config = ConfigDict(extra="allow")

        @model_validator(mode="before")
        @classmethod
        def _validate_against_schema(cls, data: Any) -> Any:
            if isinstance(data, BaseModel):
                data = data.model_dump(mode="json")
            errors = sorted(checker.iter_errors(data), key=lambda e: list(e.absolute_path))
            if errors:
                summary = "; ".join(
                    f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: "
                    f"{error.message}"
                    for error in errors[:5]
                )
                raise ValueError(f"payload does not match contract schema: {summary}")
            return data

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: D102
            return copy.deepcopy(frozen_schema)

    SchemaContractModel.__name__ = _class_name(name)
    SchemaContractModel.__qualname__ = SchemaContractModel.__name__
    SchemaContractModel.__doc__ = f"Schema-only contract {name!r}."
    return SchemaContractModel


def _class_name(contract_name: str) -> str:
    """Derive a readable class name from a contract name like ``contract://x-y``."""
    cleaned = re.sub(r"[^0-9a-zA-Z]+", " ", contract_name).title().replace(" ", "")
    return cleaned or "SchemaContract"
