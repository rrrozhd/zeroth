from __future__ import annotations

import pytest

from zeroth.mappings.errors import MappingValidationError
from zeroth.mappings.models import (
    ConstantMappingOperation,
    EdgeMapping,
    PassthroughMappingOperation,
    RenameMappingOperation,
)
from zeroth.mappings.validator import MappingValidator


def test_mapping_validator_accepts_valid_mapping() -> None:
    validator = MappingValidator()
    mapping = EdgeMapping(
        operations=[
            PassthroughMappingOperation(
                source_path="payload.user.name",
                target_path="request.user.name",
            ),
            ConstantMappingOperation(target_path="request.source", value="zeroth"),
        ]
    )

    validator.validate(mapping)


def test_mapping_validator_rejects_duplicate_targets() -> None:
    validator = MappingValidator()
    mapping = EdgeMapping(
        operations=[
            PassthroughMappingOperation(
                source_path="payload.user.name",
                target_path="request.user.name",
            ),
            RenameMappingOperation(
                source_path="payload.user.id",
                target_path="request.user.name",
            ),
        ]
    )

    with pytest.raises(MappingValidationError, match="duplicate target path"):
        validator.validate(mapping)


def test_mapping_validator_rejects_empty_paths() -> None:
    validator = MappingValidator()
    mapping = EdgeMapping(
        operations=[
            ConstantMappingOperation(target_path="", value="zeroth"),
        ]
    )

    with pytest.raises(MappingValidationError, match="target_path must not be empty"):
        validator.validate(mapping)
