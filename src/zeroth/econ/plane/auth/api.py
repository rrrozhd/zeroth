from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from zeroth.econ.plane.auth.schemas import LoginRequest, TokenResponse, UserClaims
from zeroth.econ.plane.auth.service import decode_token, issue_token
from zeroth.econ.plane.database import get_db

router = APIRouter(tags=["auth"])
security = HTTPBearer()


@router.post("/auth/token", response_model=TokenResponse)
def token(request: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    access_token = issue_token(request, db)
    return TokenResponse(access_token=access_token)


@router.get("/auth/me", response_model=UserClaims)
def me(credentials: HTTPAuthorizationCredentials = Depends(security)) -> UserClaims:
    try:
        return decode_token(credentials.credentials)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc
