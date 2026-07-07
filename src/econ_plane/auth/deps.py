from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from econ_plane.auth.schemas import UserClaims
from econ_plane.auth.service import decode_token

security = HTTPBearer()


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> UserClaims:
    try:
        return decode_token(credentials.credentials)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc


def require_roles(*allowed: str) -> Callable[[UserClaims], UserClaims]:
    def checker(user: UserClaims = Depends(get_current_user)) -> UserClaims:
        if not set(user.roles).intersection(allowed):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    return checker
