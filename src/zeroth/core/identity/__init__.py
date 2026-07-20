"""Legacy import path for the governance identity package.

The identity models live in :mod:`zeroth.governance.identity`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.governance.identity import (
    ActorIdentity,
    AuthenticatedPrincipal,
    AuthMethod,
    PrincipalScope,
    ServiceRole,
)

__all__ = [
    "ActorIdentity",
    "AuthenticatedPrincipal",
    "AuthMethod",
    "PrincipalScope",
    "ServiceRole",
]
