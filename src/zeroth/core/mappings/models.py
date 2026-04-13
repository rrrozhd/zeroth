"""Mapping schema used on graph edges.

Defines the data models that describe how values should be moved, renamed,
or filled in when data flows along an edge in the agent graph. Each model
is a Pydantic schema so it can be serialised, deserialised, and validated
automatically.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MappingOperationBase(BaseModel):
    """Base class for all mapping operations.

    Every mapping operation writes to a ``target_path`` in the output. Subclasses
    add details about where the value comes from (another field, a constant, etc.).
    """

    model_config = ConfigDict(extra="forbid")

    target_path: str


class PassthroughMappingOperation(MappingOperationBase):
    """Copy a value from the input to the output without changing it.

    The source and target paths are the same logical field, just moved across
    the edge boundary.
    """

    operation: Literal["passthrough"] = "passthrough"
    source_path: str


class RenameMappingOperation(MappingOperationBase):
    """Copy a value from the input to the output under a different name.

    Works just like passthrough, but the target path can differ from the source
    path, effectively renaming the field.
    """

    operation: Literal["rename"] = "rename"
    source_path: str


class ConstantMappingOperation(MappingOperationBase):
    """Set a target field to a fixed value, ignoring the input entirely.

    Useful when you always want a specific value in the output regardless of
    what the input contains.
    """

    operation: Literal["constant"] = "constant"
    value: Any


class DefaultMappingOperation(MappingOperationBase):
    """Copy a value from the input, falling back to a default if it is missing.

    If ``source_path`` is ``None``, the default value is always used. Otherwise
    the source is looked up first, and the default is only used when the source
    path does not exist in the input.
    """

    operation: Literal["default"] = "default"
    source_path: str | None = None
    default_value: Any


MappingOperation = Annotated[
    PassthroughMappingOperation
    | RenameMappingOperation
    | ConstantMappingOperation
    | DefaultMappingOperation,
    Field(discriminator="operation"),
]


class EdgeMapping(BaseModel):
    """A complete set of mapping operations that describe one edge's data transform.

    An ``EdgeMapping`` groups together all the individual operations that should
    run when data crosses a particular edge in the graph.
    """

    model_config = ConfigDict(extra="forbid")

    operations: list[MappingOperation] = Field(default_factory=list)
