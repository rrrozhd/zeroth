"""JWT-administered project API key lifecycle."""

from fastapi import APIRouter, Depends, HTTPException, Response, status

from zeroth.econ.plane.auth.scoped import ScopedUserClaims
from zeroth.econ.plane.cloud.auth import get_cloud_scoped_db, require_cloud_roles
from zeroth.econ.plane.cloud.keys_schemas import ApiKeyCreate, ApiKeyOut, ApiKeyReveal
from zeroth.econ.plane.cloud.keys_service import issue_api_key, list_api_keys, revoke_api_key
from zeroth.econ.plane.scoped_session import ScopedSession

router = APIRouter(tags=["zeroth-cloud-api-keys"])


@router.post("/cloud/api-keys", response_model=ApiKeyReveal)
def create_key(
    payload: ApiKeyCreate,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    user: ScopedUserClaims = Depends(require_cloud_roles("Admin")),  # noqa: B008
) -> ApiKeyReveal:
    return issue_api_key(
        db,
        payload,
        subject=user.sub,
        workspace_id=user.workspace_id,
    )


@router.get("/cloud/api-keys", response_model=list[ApiKeyOut])
def keys(
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    _user: ScopedUserClaims = Depends(require_cloud_roles("Admin")),  # noqa: B008
) -> list[ApiKeyOut]:
    return list_api_keys(db)


@router.delete("/cloud/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_key(
    key_id: str,
    db: ScopedSession = Depends(get_cloud_scoped_db),  # noqa: B008
    _user: ScopedUserClaims = Depends(require_cloud_roles("Admin")),  # noqa: B008
) -> Response:
    if not revoke_api_key(db, key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
