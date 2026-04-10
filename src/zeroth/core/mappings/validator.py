"""Validation for edge mapping definitions.

Before a mapping is executed, it should be checked for mistakes like empty
paths, badly-formatted dot paths, or two operations writing to the same target.
This module contains the logic that performs those checks.
"""

from __future__ import annotations

from zeroth.core.mappings.errors import MappingValidationError
from zeroth.core.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    MappingOperation,
    PassthroughMappingOperation,
    RenameMappingOperation,
)


def _validate_path(path: str, *, label: str) -> None:
    """Check that a dot-separated path string is well-formed.

    Raises ``MappingValidationError`` if the path is empty, has leading or
    trailing dots, or contains consecutive dots (e.g. ``"a..b"``).
    """
    if not path or not path.strip():
        msg = f"{label} must not be empty"
        raise MappingValidationError(msg)
    if path.startswith(".") or path.endswith(".") or ".." in path:
        msg = f"{label} must use dot-separated path segments"
        raise MappingValidationError(msg)


class MappingValidator:
    """Validate edge mapping definitions before execution.

    Call ``validate()`` with an ``EdgeMapping`` to make sure all its operations
    are correct.  If anything is wrong, a ``MappingValidationError`` is raised
    with a message explaining the problem.
    """

    def validate(self, mapping: EdgeMapping) -> None:
        """Validate an entire edge mapping.

        Checks that there is at least one operation, that every path is
        well-formed, and that no two operations write to the same target path.
        """
        if not mapping.operations:
            raise MappingValidationError("mapping must contain at least one operation")

        target_paths: set[str] = set()
        for operation in mapping.operations:
            self._validate_operation(operation, target_paths)

    def _validate_operation(self, operation: MappingOperation, target_paths: set[str]) -> None:
        """Validate a single mapping operation.

        Checks that every path used by the operation is well-formed and that the
        target path has not already been claimed by a previous operation.
        """
        match operation:
            case PassthroughMappingOperation():
                _validate_path(operation.source_path, label="source_path")
                _validate_path(operation.target_path, label="target_path")
                self._check_target_path(operation.target_path, target_paths)
            case RenameMappingOperation():
                _validate_path(operation.source_path, label="source_path")
                _validate_path(operation.target_path, label="target_path")
                self._check_target_path(operation.target_path, target_paths)
            case ConstantMappingOperation():
                _validate_path(operation.target_path, label="target_path")
                self._check_target_path(operation.target_path, target_paths)
            case DefaultMappingOperation():
                if operation.source_path is not None:
                    _validate_path(operation.source_path, label="source_path")
                _validate_path(operation.target_path, label="target_path")
                self._check_target_path(operation.target_path, target_paths)
            case _:
                msg = f"unsupported mapping operation: {type(operation)!r}"
                raise MappingValidationError(msg)

    def _check_target_path(self, target_path: str, target_paths: set[str]) -> None:
        """Ensure a target path has not been used by another operation.

        Raises ``MappingValidationError`` if the same target path appears more
        than once, since that would mean two operations try to write to the same
        output field.
        """
        if target_path in target_paths:
            msg = f"duplicate target path: {target_path}"
            raise MappingValidationError(msg)
        target_paths.add(target_path)
