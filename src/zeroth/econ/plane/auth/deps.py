from collections.abc import Callable, Generator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from zeroth.econ.plane.auth.scoped import ScopedUserClaims as UserClaims
from zeroth.econ.plane.auth.service import decode_token
from zeroth.econ.plane.database import get_scoped_db
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import ScopeContext, TenantWideScopeContext

security = HTTPBearer()


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> UserClaims:
    try:
        return decode_token(credentials.credentials)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc


def _claims_scope(user: UserClaims) -> ScopeContext | TenantWideScopeContext:
    if user.workspace_id is None:
        if user.tenant_id == "default":
            return TenantWideScopeContext.for_default_compatibility()
        return TenantWideScopeContext(tenant_id=user.tenant_id)
    if user.tenant_id == "default":
        return ScopeContext.for_default_compatibility(workspace_id=user.workspace_id)
    return ScopeContext(tenant_id=user.tenant_id, workspace_id=user.workspace_id)


def get_current_scoped_db(
    user: UserClaims = Depends(get_current_user),
) -> Generator[ScopedSession, None, None]:
    """Bind econ persistence to the tenant identity authenticated by the JWT."""
    yield from get_scoped_db(_claims_scope(user))


def get_current_global_db(
    _user: UserClaims = Depends(get_current_user),
) -> Generator[ScopedSession, None, None]:
    """Bind an authenticated route to the explicitly global resource scope."""
    yield from get_scoped_db(None)


def require_claimed_tenant(user: UserClaims, requested_tenant_id: str | None) -> str:
    """Return the authenticated tenant, rejecting request-selected ownership."""
    requested = "default" if requested_tenant_id == "tenant_default" else requested_tenant_id
    if requested is not None and requested != user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="requested tenant does not match authenticated tenant",
        )
    return user.tenant_id


def require_roles(*allowed: str) -> Callable[[UserClaims], UserClaims]:
    def checker(user: UserClaims = Depends(get_current_user)) -> UserClaims:
        if not set(user.roles).intersection(allowed):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    return checker
