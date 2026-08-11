from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from zeroth.econ.plane.auth.schemas import LoginRequest, TokenResponse, UserClaims
from zeroth.econ.plane.auth.service import TenantIdentityMismatchError, decode_token, issue_token
from zeroth.econ.plane.database import get_db
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import ScopeContext, TenantWideScopeContext

router = APIRouter(tags=["auth"])
security = HTTPBearer()


@router.post("/auth/token", response_model=TokenResponse)
def token(request: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    if not settings.insecure_public_token_issuer_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if request.workspace_id is None:
        scope = (
            TenantWideScopeContext.for_default_compatibility()
            if request.tenant_id == "default"
            else TenantWideScopeContext(tenant_id=request.tenant_id)
        )
    else:
        scope = (
            ScopeContext.for_default_compatibility(workspace_id=request.workspace_id)
            if request.tenant_id == "default"
            else ScopeContext(tenant_id=request.tenant_id, workspace_id=request.workspace_id)
        )
    try:
        access_token = issue_token(request, ScopedSession(db, scope))
    except TenantIdentityMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return TokenResponse(access_token=access_token)


@router.get("/auth/me", response_model=UserClaims)
def me(credentials: HTTPAuthorizationCredentials = Depends(security)) -> UserClaims:
    try:
        return decode_token(credentials.credentials)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
