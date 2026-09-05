from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select

from zeroth.econ.plane.auth.models import User
from zeroth.econ.plane.auth.scoped import ScopedLoginRequest, ScopedUserClaims
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.scoped_session import ScopedSession


class TenantIdentityMismatchError(ValueError):
    """The requested subject is not provisioned in the asserted scope."""


def issue_token(payload: ScopedLoginRequest, db: ScopedSession) -> str:
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
    if payload.workspace_id is not None and payload.workspace_id != user.workspace_id:
        raise TenantIdentityMismatchError("workspace does not match subject provisioning")

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


def decode_token(token: str) -> ScopedUserClaims:
    payload = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"verify_iat": False},
    )
    # This internal issuer does not support audience or access-token binding.
    # Preserve python-jose's rejection even for empty-valued claims.
    if "aud" in payload or "at_hash" in payload:
        raise jwt.InvalidTokenError("unsupported token binding claim")
    # Legacy tokens require numeric iat when present, but do not reject future iat.
    if "iat" in payload:
        try:
            int(payload["iat"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise jwt.InvalidTokenError("issued-at claim must be numeric") from exc
    return ScopedUserClaims(**payload)
