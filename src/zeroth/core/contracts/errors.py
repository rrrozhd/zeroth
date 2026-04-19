"""Error types for the contract registry.

These exceptions are raised when something goes wrong while registering,
looking up, or resolving contracts. They all inherit from ContractRegistryError,
so you can catch that one base class to handle any contract-related error.
"""

from __future__ import annotations


class ContractRegistryError(Exception):
    """Base error for anything that goes wrong in the contract registry.

    Catch this if you want to handle all contract-related errors in one place.
    More specific errors below inherit from this one.
    """


class ContractNotFoundError(ContractRegistryError):
    """Raised when a requested contract version is missing.

    This is raised when a lookup by contract name and version cannot find a
    matching registry entry.
    """


class ContractVersionExistsError(ContractRegistryError):
    """Raised when a contract version is registered twice.

    Each ``(name, version)`` pair must be unique within the registry.
    """


class ContractTypeResolutionError(ContractRegistryError):
    """Raised when a stored contract path cannot be resolved.

    This usually means the referenced module or class was moved, renamed, or
    deleted after the contract was registered.
    """
