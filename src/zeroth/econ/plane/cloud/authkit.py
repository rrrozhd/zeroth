"""Hosted AuthKit login, organization bootstrap, and sealed browser session."""

from __future__ import annotations

import base64
from collections.abc import Mapping
import hashlib
import json
import secrets
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from zeroth.econ.plane.cloud.activation import VerifiedWorkOSIdentity, activate_trial
from zeroth.econ.plane.config import settings
from zeroth.econ.plane.database import get_db

router = APIRouter(prefix="/cloud/auth", tags=["zeroth-cloud-auth"])

_FLOW_COOKIE = "zeroth_auth_flow"
_SESSION_COOKIE = "zeroth_session"
_FLOW_TTL_SECONDS = 600


class WorkOSGateway(Protocol):
    def authorization_url(self, **kwargs: object) -> str: ...

    def authenticate_with_code(self, *, code: str, code_verifier: str) -> Any: ...

    def create_organization(self, *, name: str, external_id: str) -> str: ...

    def add_organization_member(
        self, *, user_id: str, organization_id: str, role: str
    ) -> None: ...

    def switch_organization(self, *, refresh_token: str, organization_id: str) -> Any: ...

    def seal_session(self, auth: Any) -> str: ...

    def authenticate_session(self, sealed_session: str) -> Any: ...


class WorkOSSDKGateway:
    """Thin adapter around the optional official WorkOS Python SDK."""

    def __init__(self) -> None:
        try:
            from workos import WorkOSClient
        except ImportError as exc:  # pragma: no cover - deployment packaging guard
            raise RuntimeError("Install zeroth[cloud] to enable WorkOS AuthKit") from exc
        self._client = WorkOSClient(
            api_key=settings.workos_api_key,
            client_id=settings.workos_client_id,
        )

    def authorization_url(self, **kwargs: object) -> str:
        return self._client.user_management.get_authorization_url(**kwargs)

    def authenticate_with_code(self, *, code: str, code_verifier: str) -> Any:
        return self._client.user_management.authenticate_with_code(
            code=code,
            code_verifier=code_verifier,
        )

    def create_organization(self, *, name: str, external_id: str) -> str:
        organization = self._client.organizations.create_organization(
            name=name,
            external_id=external_id,
        )
        return organization.id

    def add_organization_member(
        self, *, user_id: str, organization_id: str, role: str
    ) -> None:
        self._client.organization_membership.create_organization_membership(
            user_id=user_id,
            organization_id=organization_id,
            role=role,
        )

    def switch_organization(self, *, refresh_token: str, organization_id: str) -> Any:
        return self._client.user_management.authenticate_with_refresh_token(
            refresh_token=refresh_token,
            organization_id=organization_id,
        )

    def seal_session(self, auth: Any) -> str:
        from workos.session import seal_session_from_auth_response

        user = auth.user.to_dict()
        impersonator = auth.impersonator.to_dict() if auth.impersonator else None
        return seal_session_from_auth_response(
            access_token=auth.access_token,
            refresh_token=auth.refresh_token,
            user=user,
            impersonator=impersonator,
            cookie_password=settings.workos_cookie_password,
        )

    def authenticate_session(self, sealed_session: str) -> Any:
        session = self._client.user_management.load_sealed_session(
            session_data=sealed_session,
            cookie_password=settings.workos_cookie_password,
        )
        return session.authenticate()


def get_workos_gateway() -> WorkOSGateway:
    return WorkOSSDKGateway()


def require_authkit_enabled() -> None:
    if not settings.workos_authkit_enabled:
        raise HTTPException(status_code=404, detail="Hosted identity is disabled")


def _flow_cipher() -> Fernet:
    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.workos_cookie_password.encode()).digest()
    )
    return Fernet(key)


def _seal_flow(*, state: str, verifier: str) -> str:
    payload = json.dumps({"state": state, "verifier": verifier}, separators=(",", ":"))
    return _flow_cipher().encrypt(payload.encode()).decode()


def _open_flow(token: str, expected_state: str) -> str:
    try:
        raw = _flow_cipher().decrypt(token.encode(), ttl=_FLOW_TTL_SECONDS)
        payload = json.loads(raw)
        state = payload["state"]
        verifier = payload["verifier"]
    except (InvalidToken, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired authentication state",
        ) from exc
    if not secrets.compare_digest(str(state), expected_state):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired authentication state",
        )
    return str(verifier)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _user_value(user: Any, field: str) -> Any:
    if isinstance(user, Mapping):
        return user[field]
    return getattr(user, field)


def _organization_name(user: Any) -> str:
    first = getattr(user, "first_name", None)
    last = getattr(user, "last_name", None)
    name = " ".join(part for part in (first, last) if part)
    return name or str(_user_value(user, "email")).split("@", maxsplit=1)[0]


@router.get("/login")
def login(
    _enabled: None = Depends(require_authkit_enabled),  # noqa: B008
    gateway: WorkOSGateway = Depends(get_workos_gateway),  # noqa: B008
) -> RedirectResponse:
    state_token = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    url = gateway.authorization_url(
        provider="authkit",
        redirect_uri=settings.workos_redirect_uri,
        state=state_token,
        code_challenge_method="S256",
        code_challenge=_pkce_challenge(verifier),
    )
    response = RedirectResponse(url=url)
    response.set_cookie(
        _FLOW_COOKIE,
        _seal_flow(state=state_token, verifier=verifier),
        max_age=_FLOW_TTL_SECONDS,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/v1/cloud/auth/callback",
    )
    return response


@router.get("/callback")
def callback(
    request: Request,
    code: str = Query(min_length=1),
    state_token: str = Query(alias="state", min_length=1),
    _enabled: None = Depends(require_authkit_enabled),  # noqa: B008
    gateway: WorkOSGateway = Depends(get_workos_gateway),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> Response:
    flow_cookie = request.cookies.get(_FLOW_COOKIE, "")
    verifier = _open_flow(flow_cookie, state_token)
    auth = gateway.authenticate_with_code(code=code, code_verifier=verifier)
    user = auth.user
    organization_id = auth.organization_id
    if not organization_id:
        user_id = str(_user_value(user, "id"))
        organization_id = gateway.create_organization(
            name=_organization_name(user),
            external_id=f"zeroth-user-{user_id}",
        )
        gateway.add_organization_member(
            user_id=user_id,
            organization_id=organization_id,
            role="admin",
        )
        auth = gateway.switch_organization(
            refresh_token=auth.refresh_token,
            organization_id=organization_id,
        )
        user = auth.user

    activation = activate_trial(
        db,
        VerifiedWorkOSIdentity(
            external_user_id=str(_user_value(user, "id")),
            external_organization_id=organization_id,
            email=str(_user_value(user, "email")),
            email_verified=bool(_user_value(user, "email_verified")),
        ),
    )
    if "text/html" in request.headers.get("accept", "").lower():
        from zeroth.econ.plane.cloud.web import activation_page

        response = activation_page(
            tenant_id=activation.tenant_id,
            key_id=activation.key_id,
            api_key=activation.api_key,
        )
    else:
        response = JSONResponse(
            {
                "tenant_id": activation.tenant_id,
                "key_id": activation.key_id,
                "api_key": activation.api_key,
                "api_key_revealed_once": activation.api_key is not None,
            }
        )
    response.delete_cookie(_FLOW_COOKIE, path="/v1/cloud/auth/callback")
    response.set_cookie(
        _SESSION_COOKIE,
        gateway.seal_session(auth),
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response
