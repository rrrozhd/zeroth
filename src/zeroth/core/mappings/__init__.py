"""Edge mapping schema, validation, and execution.

This package provides tools to define how data flows between nodes in a graph.
You can describe what fields to copy, rename, set to fixed values, or give
defaults, then validate and run those mappings against real data.
"""

from zeroth.core.mappings.errors import MappingExecutionError
from zeroth.core.mappings.executor import MappingExecutor
from zeroth.core.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    MappingOperation,
    PassthroughMappingOperation,
    RenameMappingOperation,
    TransformMappingOperation,
)
from zeroth.core.mappings.validator import MappingValidationError, MappingValidator

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
