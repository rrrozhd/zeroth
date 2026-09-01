"""Authenticate hosted routes with either UI JWTs or project API keys."""

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import UTC, datetime
import hashlib

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.auth.service import decode_token
from zeroth.econ.plane.cloud.models import CloudApiKey
from zeroth.econ.plane.database import get_db
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import ScopeContext, TenantWideScopeContext

security = HTTPBearer()


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
    credentials: HTTPAuthorizationCredentials = Depends(security),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ScopedUserClaims:
    try:
        return decode_token(credentials.credentials)
    except Exception:  # noqa: BLE001
        claims = _api_key_claims(credentials.credentials, db)
        if claims is not None:
            return claims
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


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
