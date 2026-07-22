"""Legacy import path for :mod:`zeroth.contracts.registry.registry`.

The error classes were importable from this module's namespace alongside the
registry types, so the shim republishes both.
"""

from zeroth.contracts.registry.errors import (
    ContractNotFoundError,
    ContractTypeResolutionError,
    ContractVersionExistsError,
)
from zeroth.contracts.registry.registry import (
    ContractReference,
    ContractRegistry,
    ContractVersion,
    StepContractBinding,
    ToolContractBinding,
    validate_artifact_reference,
)

__all__ = [
    "ContractNotFoundError",
    "ContractReference",
    "ContractRegistry",
    "ContractTypeResolutionError",
    "ContractVersion",
    "ContractVersionExistsError",
    "StepContractBinding",
    "ToolContractBinding",
    "validate_artifact_reference",
]
