"""Mapping-related errors.

Custom exception classes used when something goes wrong with edge mappings.
"""


class MappingValidationError(ValueError):
    """Raised when an edge mapping definition is invalid.

    For example, this is raised if a mapping has an empty path, a duplicate
    target, or no operations at all. It inherits from ValueError so you can
    catch it with a broad ``except ValueError`` if needed.
    """


class MappingExecutionError(ValueError):
    """Raised when a mapping operation fails during execution.

    For example, this is raised when a transform expression encounters a
    division by zero, type error, or references an unsupported AST construct.
    """
