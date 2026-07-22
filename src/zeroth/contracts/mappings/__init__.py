"""Data mapping contracts.

This package provides tools to define how data flows between nodes in a graph.
You can describe what fields to copy, rename, set to fixed values, or give
defaults, then validate and run those mappings against real data.
"""

from zeroth.contracts.mappings.errors import MappingExecutionError
from zeroth.contracts.mappings.executor import MappingExecutor
from zeroth.contracts.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    MappingOperation,
    PassthroughMappingOperation,
    RenameMappingOperation,
    TransformMappingOperation,
)
from zeroth.contracts.mappings.validator import MappingValidationError, MappingValidator

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
