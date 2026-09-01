from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from zeroth.econ.plane.cloud.models import CloudApiKey, CloudIdentityMembership
from zeroth.econ.plane.database import Base, get_db


@dataclass
class _User:
    id: str = "user_01"
    email: str = "owner@example.com"
    email_verified: bool = True
    first_name: str | None = "Ada"
    last_name: str | None = "Lovelace"


@dataclass
class _Auth:
    user: _User
    access_token: str = "access"
    refresh_token: str = "refresh"
    organization_id: str | None = "org_01"
    impersonator: object | None = None


@dataclass
class _SessionAuth:
    authenticated: bool = True
    organization_id: str | None = "org_01"
    role: str | None = "admin"
    roles: list[str] | None = None
    user: dict[str, object] | None = None


class _FakeWorkOS:
    def __init__(self, *, organization_id: str | None = "org_01") -> None:
        self.organization_id = organization_id
        self.authorization_kwargs: dict[str, object] = {}
        self.created_orgs: list[tuple[str, str]] = []
        self.memberships: list[tuple[str, str, str]] = []
        self.switches: list[tuple[str, str]] = []
        self.session_auth = _SessionAuth(user={"id": "user_01", "email": "owner@example.com"})

    def authorization_url(self, **kwargs: object) -> str:
        self.authorization_kwargs = kwargs
        return "https://auth.example.test/authorize"

    def authenticate_with_code(self, *, code: str, code_verifier: str) -> _Auth:
        assert code == "valid-code"
        assert code_verifier
        return _Auth(user=_User(), organization_id=self.organization_id)

    def create_organization(self, *, name: str, external_id: str) -> str:
        self.created_orgs.append((name, external_id))
        return "org_created"

    def add_organization_member(self, *, user_id: str, organization_id: str, role: str) -> None:
        self.memberships.append((user_id, organization_id, role))

    def switch_organization(self, *, refresh_token: str, organization_id: str) -> _Auth:
        self.switches.append((refresh_token, organization_id))
        return _Auth(user=_User(), organization_id=organization_id)

    def seal_session(self, auth: _Auth) -> str:
        assert auth.organization_id
        return "sealed-session"

    def authenticate_session(self, sealed_session: str) -> _SessionAuth:
        assert sealed_session == "sealed-session"
        return self.session_auth


def _client(tmp_path: Path, monkeypatch, gateway: _FakeWorkOS) -> tuple[TestClient, sessionmaker]:
    from zeroth.econ.plane.cloud.authkit import get_workos_gateway, router
    from zeroth.econ.plane.config import settings

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'authkit.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, class_=Session)

    app = FastAPI()
    app.include_router(router, prefix="/v1")

    def db_override():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_workos_gateway] = lambda: gateway
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    monkeypatch.setattr(settings, "workos_api_key", "sk_test")
    monkeypatch.setattr(settings, "workos_redirect_uri", "https://api.example.test/v1/cloud/auth/callback")
    monkeypatch.setattr(settings, "workos_cookie_password", "x" * 32)
    monkeypatch.setattr(settings, "cloud_browser_origin", "https://app.example.test")
    monkeypatch.setattr(settings, "workos_authkit_enabled", True)
    return TestClient(app, base_url="https://api.example.test"), sessions


def test_login_redirects_to_authkit_with_state_and_pkce_in_secure_cookie(
    tmp_path: Path, monkeypatch
) -> None:
    gateway = _FakeWorkOS()
    client, _ = _client(tmp_path, monkeypatch, gateway)

    response = client.get("/v1/cloud/auth/login", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://auth.example.test/authorize"
    assert gateway.authorization_kwargs["provider"] == "authkit"
    assert gateway.authorization_kwargs["code_challenge_method"] == "S256"
    assert gateway.authorization_kwargs["state"]
    assert gateway.authorization_kwargs["code_challenge"]
    cookie = response.headers["set-cookie"]
    assert "zeroth_auth_flow=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie


def test_authkit_route_is_inert_when_hosted_identity_is_disabled(tmp_path: Path, monkeypatch) -> None:
    from zeroth.econ.plane.config import settings

    gateway = _FakeWorkOS()
    client, _ = _client(tmp_path, monkeypatch, gateway)
    monkeypatch.setattr(settings, "workos_authkit_enabled", False)

    response = client.get("/v1/cloud/auth/login", follow_redirects=False)

    assert response.status_code == 404
    assert gateway.authorization_kwargs == {}


def test_callback_rejects_state_mismatch_before_code_exchange(tmp_path: Path, monkeypatch) -> None:
    gateway = _FakeWorkOS()
    client, _ = _client(tmp_path, monkeypatch, gateway)
    client.get("/v1/cloud/auth/login", follow_redirects=False)

    response = client.get("/v1/cloud/auth/callback?code=valid-code&state=wrong")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired authentication state"


def test_callback_activates_org_trial_and_sets_workos_session(tmp_path: Path, monkeypatch) -> None:
    gateway = _FakeWorkOS()
    client, sessions = _client(tmp_path, monkeypatch, gateway)
    client.get("/v1/cloud/auth/login", follow_redirects=False)
    state = str(gateway.authorization_kwargs["state"])

    response = client.get(f"/v1/cloud/auth/callback?code=valid-code&state={state}")

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"].startswith("ten_")
    assert body["api_key"].startswith("zth_live_")
    assert body["api_key_revealed_once"] is True
    session_cookie = response.headers["set-cookie"]
    assert "zeroth_session=sealed-session" in session_cookie
    assert "HttpOnly" in session_cookie and "Secure" in session_cookie
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(CloudApiKey)) == 1
        membership = db.scalar(select(CloudIdentityMembership))
        assert membership is not None
        assert membership.external_organization_id == "org_01"


def test_browser_callback_renders_one_time_activation_instead_of_raw_json(
    tmp_path: Path, monkeypatch
) -> None:
    gateway = _FakeWorkOS()
    client, _ = _client(tmp_path, monkeypatch, gateway)
    client.get("/v1/cloud/auth/login", follow_redirects=False)
    state = str(gateway.authorization_kwargs["state"])

    response = client.get(
        f"/v1/cloud/auth/callback?code=valid-code&state={state}",
        headers={"Accept": "text/html,application/xhtml+xml"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "Your Zeroth trial is ready" in response.text
    assert "zth_live_" in response.text
    assert "$39/month" in response.text
    assert 'action="/account/checkout"' in response.text
    assert 'href="/account"' in response.text


def test_first_login_creates_workos_org_and_admin_membership(tmp_path: Path, monkeypatch) -> None:
    gateway = _FakeWorkOS(organization_id=None)
    client, _ = _client(tmp_path, monkeypatch, gateway)
    client.get("/v1/cloud/auth/login", follow_redirects=False)
    state = str(gateway.authorization_kwargs["state"])

    response = client.get(f"/v1/cloud/auth/callback?code=valid-code&state={state}")

    assert response.status_code == 200
    assert gateway.created_orgs == [("Ada Lovelace", "zeroth-user-user_01")]
    assert gateway.memberships == [("user_01", "org_created", "admin")]
    assert gateway.switches == [("refresh", "org_created")]


def _protected_client(tmp_path: Path, monkeypatch, gateway: _FakeWorkOS) -> TestClient:
    from fastapi import Depends

    from zeroth.econ.plane.cloud import authkit
    from zeroth.econ.plane.cloud.activation import VerifiedWorkOSIdentity, activate_trial
    from zeroth.econ.plane.cloud.auth import get_cloud_user
    from zeroth.econ.plane.config import settings

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'protected.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, class_=Session)
    with sessions() as db:
        activate_trial(
            db,
            VerifiedWorkOSIdentity(
                external_user_id="user_01",
                external_organization_id="org_01",
                email="owner@example.com",
                email_verified=True,
            ),
        )

    app = FastAPI()

    def db_override():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = db_override
    monkeypatch.setattr(authkit, "get_workos_gateway", lambda: gateway)
    monkeypatch.setattr(settings, "workos_authkit_enabled", True)
    monkeypatch.setattr(settings, "workos_cookie_password", "x" * 32)
    monkeypatch.setattr(settings, "cloud_browser_origin", "https://app.example.test")

    @app.api_route("/protected", methods=["GET", "POST"])
    def protected(user=Depends(get_cloud_user)):  # noqa: B008
        return user.model_dump()

    client = TestClient(app, base_url="https://api.example.test")
    client.cookies.set("zeroth_session", "sealed-session")
    return client


def test_sealed_workos_session_resolves_trusted_tenant_and_role(tmp_path: Path, monkeypatch) -> None:
    gateway = _FakeWorkOS()
    client = _protected_client(tmp_path, monkeypatch, gateway)

    response = client.get("/protected")

    assert response.status_code == 200
    assert response.json()["tenant_id"].startswith("ten_")
    assert response.json()["roles"] == ["Admin"]
    assert response.json()["sub"] == "workos:user_01"


def test_unknown_workos_role_fails_closed(tmp_path: Path, monkeypatch) -> None:
    gateway = _FakeWorkOS()
    gateway.session_auth.role = "mystery-role"
    client = _protected_client(tmp_path, monkeypatch, gateway)

    response = client.get("/protected")

    assert response.status_code == 403
    assert response.json()["detail"] == "Unsupported WorkOS role"


def test_workos_session_without_local_org_membership_is_rejected(tmp_path: Path, monkeypatch) -> None:
    gateway = _FakeWorkOS()
    gateway.session_auth.organization_id = "org_attacker"
    client = _protected_client(tmp_path, monkeypatch, gateway)

    response = client.get("/protected")

    assert response.status_code == 401


def test_cookie_authenticated_mutation_requires_exact_browser_origin(
    tmp_path: Path, monkeypatch
) -> None:
    gateway = _FakeWorkOS()
    client = _protected_client(tmp_path, monkeypatch, gateway)

    missing = client.post("/protected")
    wrong = client.post("/protected", headers={"Origin": "https://evil.example"})
    exact = client.post(
        "/protected",
        headers={"Origin": "https://app.example.test"},
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert exact.status_code == 200


def test_authkit_routes_are_mounted_on_the_plane() -> None:
    from zeroth.econ.plane.main import app

    paths = app.openapi()["paths"]
    assert "/v1/cloud/auth/login" in paths
    assert "/v1/cloud/auth/callback" in paths
