"""Production contract for tenant-bound non-relational persistence drivers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from zeroth.platform.storage.scoping import ResourceOperation, ResourceScopeDefinition

type ScopedOperation = Callable[..., Awaitable[Any]]


@runtime_checkable
class ScopedResourceDriver(Protocol):
    """A scope-bound production gateway with immutable operation discovery."""

    @property
    def resource_definition(self) -> ResourceScopeDefinition: ...

    @property
    def operations(self) -> Mapping[ResourceOperation, ScopedOperation]: ...


__all__ = ["ScopedOperation", "ScopedResourceDriver"]
