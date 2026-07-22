"""Legacy import path for the contract registry package.

The contract registry lives in :mod:`zeroth.contracts.registry`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.contracts.registry import (
    ContractNotFoundError,
    ContractReference,
    ContractRegistry,
    ContractRegistryError,
    ContractVersion,
    StepContractBinding,
    ToolContractBinding,
    validate_artifact_reference,
)

__all__ = [
    "ContractNotFoundError",
    "ContractReference",
    "ContractRegistry",
    "ContractRegistryError",
    "ContractVersion",
    "StepContractBinding",
    "ToolContractBinding",
    "validate_artifact_reference",
]
