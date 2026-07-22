"""Legacy import path for the mappings contracts package.

Edge mappings live in :mod:`zeroth.contracts.mappings`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.contracts.mappings import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    MappingExecutionError,
    MappingExecutor,
    MappingOperation,
    MappingValidationError,
    MappingValidator,
    PassthroughMappingOperation,
    RenameMappingOperation,
    TransformMappingOperation,
)

__all__ = [
    "ConstantMappingOperation",
    "DefaultMappingOperation",
    "EdgeMapping",
    "MappingExecutionError",
    "MappingExecutor",
    "MappingOperation",
    "MappingValidationError",
    "MappingValidator",
    "PassthroughMappingOperation",
    "RenameMappingOperation",
    "TransformMappingOperation",
]
