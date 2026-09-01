"""Issue, enumerate, and revoke high-entropy project API keys."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import secrets
import uuid

from sqlalchemy import select

from zeroth.econ.plane.cloud.keys_schemas import ApiKeyCreate, ApiKeyOut, ApiKeyReveal
from zeroth.econ.plane.cloud.models import CloudApiKey
from zeroth.econ.plane.scoped_session import ScopedSession


def _out(row: CloudApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        key_id=row.key_id,
        name=row.name,
        last_four=row.last_four,
        roles=list(row.roles_json),
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


def _new_api_key(
    *,
    tenant_id: str,
    payload: ApiKeyCreate,
    subject: str,
    workspace_id: str | None,
    now: datetime,
) -> tuple[CloudApiKey, ApiKeyReveal]:
    key_id = f"key_{uuid.uuid4().hex[:20]}"
    secret = secrets.token_urlsafe(32)
    api_key = f"zth_live_{key_id[4:12]}_{secret}"
    row = CloudApiKey(
        key_id=key_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        name=payload.name,
        secret_hash=hashlib.sha256(api_key.encode()).hexdigest(),
        last_four=api_key[-4:],
        roles_json=payload.roles,
        created_by=subject,
        created_at=now,
        last_used_at=None,
        revoked_at=None,
    )
    return row, ApiKeyReveal(**_out(row).model_dump(), api_key=api_key)


def issue_api_key(
    db: ScopedSession,
    payload: ApiKeyCreate,
    *,
    subject: str,
    workspace_id: str | None,
) -> ApiKeyReveal:
    if type(db) is not ScopedSession or db.scope is None:
        raise TypeError("project API keys require a tenant-scoped session")
    now = datetime.now(UTC)
    row, reveal = _new_api_key(
        tenant_id=db.scope.tenant_id,
        payload=payload,
        subject=subject,
        workspace_id=workspace_id,
        now=now,
    )
    db.add(row)
    db.commit()
    return reveal


def list_api_keys(db: ScopedSession) -> list[ApiKeyOut]:
    if type(db) is not ScopedSession:
        raise TypeError("project API keys require a ScopedSession")
    rows = list(db.scalars(select(CloudApiKey).order_by(CloudApiKey.created_at.desc())))
    return [_out(row) for row in rows]


def revoke_api_key(db: ScopedSession, key_id: str) -> bool:
    if type(db) is not ScopedSession:
        raise TypeError("project API keys require a ScopedSession")
    row = db.get(CloudApiKey, key_id)
    if row is None:
        return False
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        db.commit()
    return True
