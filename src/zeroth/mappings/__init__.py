"""Edge mapping schema, validation, and execution.

This package provides tools to define how data flows between nodes in a graph.
You can describe what fields to copy, rename, set to fixed values, or give
defaults, then validate and run those mappings against real data.
"""

from zeroth.mappings.executor import MappingExecutor
from zeroth.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    MappingOperation,
    PassthroughMappingOperation,
    RenameMappingOperation,
)
from zeroth.mappings.validator import MappingValidationError, MappingValidator

__all__ = [
    "ConstantMappingOperation",
    "DefaultMappingOperation",
    "EdgeMapping",
    "MappingExecutor",
    "MappingOperation",
    "MappingValidationError",
    "MappingValidator",
    "PassthroughMappingOperation",
    "RenameMappingOperation",
]
