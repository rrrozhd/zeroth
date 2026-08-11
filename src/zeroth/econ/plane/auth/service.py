from datetime import datetime, timedelta, timezone

from jose import jwt
from sqlalchemy import select

from zeroth.econ.plane.auth.models import User
from zeroth.econ.plane.auth.schemas import LoginRequest, UserClaims
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.scoped_session import ScopedSession


class TenantIdentityMismatchError(ValueError):
    """The requested subject is not provisioned in the asserted scope."""


def issue_token(payload: LoginRequest, db: ScopedSession) -> str:
    """Issue a legacy development token inside an already-bound scope.

    The HTTP caller's tenant assertion only selects the structural scope; it
    never widens a query. A subject provisioned under another tenant therefore
    remains invisible and cannot be minted into that tenant.
    """
    user = db.execute(select(User).where(User.subject == payload.sub)).scalar_one_or_none()
    if user is None:
        raise TenantIdentityMismatchError("subject is not provisioned in the requested tenant")
    if user.email != str(payload.email):
        raise TenantIdentityMismatchError("subject identity does not match provisioning")

    exp = datetime.now(tz=timezone.utc) + timedelta(hours=12)
    claims = {
        "sub": user.subject,
        "email": user.email,
        "roles": payload.roles,
        "tenant_id": user.tenant_id,
        "workspace_id": user.workspace_id,
        "iss": "econ-plane",
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> UserClaims:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    return UserClaims(**payload)
