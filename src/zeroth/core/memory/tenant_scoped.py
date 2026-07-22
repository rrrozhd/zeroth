"""Legacy import path for :mod:`zeroth.integrations.memory.tenant_scoped`."""

from zeroth.integrations.memory.tenant_scoped import (
    TenantScopedMemoryConnector,
    TenantScopeError,
    tenant_slug,
)

__all__ = [
    "TenantScopeError",
    "TenantScopedMemoryConnector",
    "tenant_slug",
]
