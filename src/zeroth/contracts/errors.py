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
    """Raised when you try to look up a contract (by name and version) that
    doesn't exist in the registry.
    """


class ContractVersionExistsError(ContractRegistryError):
    """Raised when you try to register a contract version that has already
    been registered. Each (name, version) pair must be unique.
    """


class ContractTypeResolutionError(ContractRegistryError):
    """Raised when the registry can't turn a stored contract path back into
    an actual Python class. This usually means the module or class was moved,
    renamed, or deleted since the contract was registered.
    """
