"""Authenticate hosted routes with either UI JWTs or project API keys."""

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import UTC, datetime
import hashlib
import hmac

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.auth.service import decode_token
from zeroth.econ.plane.cloud.models import CloudApiKey, CloudIdentityMembership
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import get_db
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import ScopeContext, TenantWideScopeContext

security = HTTPBearer(auto_error=False)

_WORKOS_ROLES = {
    "admin": "Admin",
    "analyst": "Analyst",
    "approver": "Approver",
    "viewer": "Viewer",
}


def _api_key_claims(token: str, db: Session) -> ScopedUserClaims | None:
    if not token.startswith("zth_live_"):
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    table = CloudApiKey.__table__
    row = db.execute(
        select(
            table.c.key_id,
            table.c.tenant_id,
            table.c.workspace_id,
            table.c.roles_json,
        ).where(table.c.secret_hash == digest, table.c.revoked_at.is_(None))
    ).mappings().one_or_none()
    if row is None:
        return None
    db.execute(
        update(table)
        .where(table.c.key_id == row["key_id"], table.c.revoked_at.is_(None))
        .values(last_used_at=datetime.now(UTC))
    )
    db.commit()
    return ScopedUserClaims(
        sub=f"api-key:{row['key_id']}",
        email="api-key@zeroth.dev",
        roles=list(row["roles_json"]),
        tenant_id=row["tenant_id"],
        workspace_id=row["workspace_id"],
        exp=253402300799,
        iss="zeroth-cloud-key",
    )


def get_cloud_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ScopedUserClaims:
    if credentials is not None:
        try:
            return decode_token(credentials.credentials)
        except Exception:  # noqa: BLE001
            claims = _api_key_claims(credentials.credentials, db)
            if claims is not None:
                return claims
    sealed_session = request.cookies.get("zeroth_session")
    if settings.workos_authkit_enabled and sealed_session:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin", "")
            if not origin or not hmac.compare_digest(origin, settings.cloud_browser_origin):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid browser origin",
                )
        return _workos_session_claims(sealed_session, db)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def _workos_session_claims(sealed_session: str, db: Session) -> ScopedUserClaims:
    # Imported at call time so headless OSS and project-key requests do not load
    # or require the hosted WorkOS dependency.
    from zeroth.econ.plane.cloud import authkit

    try:
        result = authkit.get_workos_gateway().authenticate_session(sealed_session)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid WorkOS session",
        ) from exc
    organization_id = getattr(result, "organization_id", None)
    user = getattr(result, "user", None) or {}
    user_id = user.get("id")
    if not getattr(result, "authenticated", False) or not organization_id or not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid WorkOS session")

    membership = db.scalar(
        select(CloudIdentityMembership).where(
            CloudIdentityMembership.provider == "workos",
            CloudIdentityMembership.external_organization_id == organization_id,
            CloudIdentityMembership.external_user_id == user_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid WorkOS session")

    external_roles = list(getattr(result, "roles", None) or [])
    if getattr(result, "role", None):
        external_roles.append(result.role)
    roles = sorted({_WORKOS_ROLES[role] for role in external_roles if role in _WORKOS_ROLES})
    if not roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unsupported WorkOS role",
        )
    return ScopedUserClaims(
        sub=f"workos:{user_id}",
        email=str(user.get("email") or membership.email),
        roles=roles,
        tenant_id=membership.tenant_id,
        workspace_id=None,
        exp=253402300799,
        iss="workos-authkit",
    )


def _scope(user: ScopedUserClaims) -> ScopeContext | TenantWideScopeContext:
    if user.workspace_id is None:
        return (
            TenantWideScopeContext.for_default_compatibility()
            if user.tenant_id == "default"
            else TenantWideScopeContext(tenant_id=user.tenant_id)
        )
    return (
        ScopeContext.for_default_compatibility(workspace_id=user.workspace_id)
        if user.tenant_id == "default"
        else ScopeContext(tenant_id=user.tenant_id, workspace_id=user.workspace_id)
    )


def get_cloud_scoped_db(
    user: ScopedUserClaims = Depends(get_cloud_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> Generator[ScopedSession, None, None]:
    yield ScopedSession(db, _scope(user))


def require_cloud_roles(*allowed: str) -> Callable[[ScopedUserClaims], ScopedUserClaims]:
    def checker(
        user: ScopedUserClaims = Depends(get_cloud_user),  # noqa: B008
    ) -> ScopedUserClaims:
        if not set(user.roles).intersection(allowed):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    return checker
