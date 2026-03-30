"""Execute edge mappings against structured payloads.

This module takes a validated set of mapping operations and applies them to an
input dictionary (the payload) to produce a new output dictionary. Think of it
as a small data-transformation engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zeroth.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    MappingOperation,
    PassthroughMappingOperation,
    RenameMappingOperation,
)
from zeroth.mappings.validator import MappingValidator


def _get_path(payload: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    """Look up a value inside a nested dictionary using a dot-separated path.

    Returns a tuple of (found, value).  If the path does not exist, returns
    (False, None) instead of raising an error.
    """
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
    """Set a value inside a nested dictionary using a dot-separated path.

    Intermediate dictionaries are created automatically if they don't exist yet.
    For example, ``_set_path({}, "a.b.c", 1)`` produces ``{"a": {"b": {"c": 1}}}``.
    """
    current = payload
    parts = path.split(".")
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


class MappingExecutor:
    """Apply validated mapping operations sequentially.

    Use this class when you want to transform an input dictionary into an output
    dictionary according to a set of mapping rules (an ``EdgeMapping``).  The
    executor first validates the mapping and then runs each operation in order.
    """

    def __init__(self, validator: MappingValidator | None = None):
        self._validator = validator or MappingValidator()

    def execute(self, payload: Mapping[str, Any], mapping: EdgeMapping) -> dict[str, Any]:
        """Run all mapping operations against the given payload and return the result.

        The payload is the input data (read-only). A brand-new dictionary is
        built up by applying each operation and then returned.
        """
        self._validator.validate(mapping)
        output: dict[str, Any] = {}
        for operation in mapping.operations:
            self._apply_operation(operation, payload, output)
        return output

    def _apply_operation(
        self,
        operation: MappingOperation,
        payload: Mapping[str, Any],
        output: dict[str, Any],
    ) -> None:
        """Apply a single mapping operation, reading from payload and writing to output.

        Handles each operation type: passthrough (copy as-is), rename (copy to a
        different key), constant (set a fixed value), and default (use a fallback
        if the source is missing).
        """
        match operation:
            case PassthroughMappingOperation():
                exists, value = _get_path(payload, operation.source_path)
                if exists:
                    _set_path(output, operation.target_path, value)
            case RenameMappingOperation():
                exists, value = _get_path(payload, operation.source_path)
                if exists:
                    _set_path(output, operation.target_path, value)
            case ConstantMappingOperation():
                _set_path(output, operation.target_path, operation.value)
            case DefaultMappingOperation():
                if operation.source_path is None:
                    _set_path(output, operation.target_path, operation.default_value)
                    return
                exists, value = _get_path(payload, operation.source_path)
                _set_path(
                    output,
                    operation.target_path,
                    value if exists else operation.default_value,
                )
