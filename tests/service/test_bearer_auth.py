from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tests.service.helpers import approval_resume_graph, deploy_service
from zeroth.governance.identity import ServiceRole
from zeroth.service.api import authentication
from zeroth.service.api.authentication import BearerTokenConfig, ServiceAuthConfig
from zeroth.service.api.authentication import AuthenticationError, JWTBearerTokenVerifier
from zeroth.core.service.bootstrap import bootstrap_app


def _bearer_auth_fixture(*, kid: str = "test-key") -> tuple[ServiceAuthConfig, object]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    jwk["kid"] = kid
    config = ServiceAuthConfig(
        bearer=BearerTokenConfig(
            issuer="https://issuer.example.test",
            audience="zeroth-service",
            jwks={"keys": [jwk]},
        )
    )
    return config, private_key


def _remote_bearer_auth_fixture(
    *, kid: str = "test-key"
) -> tuple[ServiceAuthConfig, object, dict[str, object]]:
    config, private_key = _bearer_auth_fixture(kid=kid)
    assert config.bearer is not None
    jwks = config.bearer.jwks
    assert jwks is not None
    remote_config = ServiceAuthConfig(
        bearer=config.bearer.model_copy(
            update={"jwks": None, "jwks_url": "https://issuer.example.test/jwks"}
        )
    )
    return remote_config, private_key, jwks


class _JWKSResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self) -> _JWKSResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]


def _encode_token(
    private_key,
    *,
    include_exp: bool = True,
    kid: str = "test-key",
    **claims: object,
) -> str:
    payload = {
        "sub": "reviewer-bearer",
        "roles": [ServiceRole.REVIEWER.value],
        "tenant_id": "default",
        "iss": "https://issuer.example.test",
        "aud": "zeroth-service",
    }
    if include_exp:
        payload["exp"] = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp())
    payload.update(claims)
    return jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": kid},
    )


async def test_health_accepts_valid_bearer_token(sqlite_db) -> None:
    auth_config, private_key = _bearer_auth_fixture()
    token = _encode_token(private_key)
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-bearer-valid"),
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=auth_config,
    )

    with TestClient(app) as client:
        response = client.get("/health", headers=_token_headers(token))

    assert response.status_code == 200


async def test_runs_rejects_bearer_token_without_expiry(sqlite_db) -> None:
    auth_config, private_key = _bearer_auth_fixture()
    token = _encode_token(private_key, include_exp=False)
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-bearer-missing-expiry"),
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=auth_config,
    )

    with TestClient(app) as client:
        response = client.get("/runs", headers=_token_headers(token))

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid bearer token"}


async def test_runs_rejects_bearer_token_when_remote_jwks_is_unavailable(
    sqlite_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_config, private_key, _ = _remote_bearer_auth_fixture()
    token = _encode_token(private_key)

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise OSError("unavailable")

    monkeypatch.setattr(authentication, "urlopen", unavailable)
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-bearer-jwks-unavailable"),
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=auth_config,
    )

    with TestClient(app) as client:
        response = client.get("/runs", headers=_token_headers(token))

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid bearer token"}


def test_remote_jwks_fetch_uses_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_config, private_key, _ = _remote_bearer_auth_fixture()
    token = _encode_token(private_key)
    seen_timeout: list[float] = []

    def timeout(*_args: object, **kwargs: object) -> object:
        seen_timeout.append(float(kwargs["timeout"]))
        raise TimeoutError

    monkeypatch.setattr(authentication, "urlopen", timeout)

    with pytest.raises(AuthenticationError, match="^invalid bearer token$"):
        JWTBearerTokenVerifier(auth_config.bearer).verify(token)  # type: ignore[arg-type]

    assert len(seen_timeout) == 1
    assert 0 < seen_timeout[0] <= 5


def test_remote_jwks_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_config, private_key, jwks = _remote_bearer_auth_fixture()
    token = _encode_token(private_key)
    oversized = json.dumps({**jwks, "padding": "x" * (1024 * 1024)}).encode()
    monkeypatch.setattr(
        authentication,
        "urlopen",
        lambda *_args, **_kwargs: _JWKSResponse(oversized),
    )

    with pytest.raises(AuthenticationError, match="^invalid bearer token$"):
        JWTBearerTokenVerifier(auth_config.bearer).verify(token)  # type: ignore[arg-type]


def test_remote_jwks_rejects_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_config, private_key, _ = _remote_bearer_auth_fixture()
    token = _encode_token(private_key)
    monkeypatch.setattr(
        authentication,
        "urlopen",
        lambda *_args, **_kwargs: _JWKSResponse(b"{not-json"),
    )

    with pytest.raises(AuthenticationError, match="^invalid bearer token$"):
        JWTBearerTokenVerifier(auth_config.bearer).verify(token)  # type: ignore[arg-type]


def test_remote_jwks_cache_reuses_key_set_for_300_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_config, private_key, jwks = _remote_bearer_auth_fixture()
    token = _encode_token(private_key)
    payload = json.dumps(jwks).encode()
    calls = 0
    now = 0.0

    def fetch(*_args: object, **_kwargs: object) -> _JWKSResponse:
        nonlocal calls
        calls += 1
        return _JWKSResponse(payload)

    monkeypatch.setattr(authentication, "urlopen", fetch)
    monkeypatch.setattr(authentication.time, "monotonic", lambda: now)
    verifier = JWTBearerTokenVerifier(auth_config.bearer)  # type: ignore[arg-type]

    assert verifier.verify(token).subject == "reviewer-bearer"
    now = 299.0
    assert verifier.verify(token).subject == "reviewer-bearer"
    assert calls == 1
    now = 300.0
    assert verifier.verify(token).subject == "reviewer-bearer"
    assert calls == 2


def test_remote_jwks_cached_unknown_kid_refreshes_once_for_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_config, old_key, old_jwks = _remote_bearer_auth_fixture(kid="old-key")
    replacement_config, replacement_key = _bearer_auth_fixture(kid="replacement-key")
    assert replacement_config.bearer is not None
    replacement_jwks = replacement_config.bearer.jwks
    assert replacement_jwks is not None
    responses = iter([json.dumps(old_jwks).encode(), json.dumps(replacement_jwks).encode()])
    calls = 0

    def fetch(*_args: object, **_kwargs: object) -> _JWKSResponse:
        nonlocal calls
        calls += 1
        return _JWKSResponse(next(responses))

    monkeypatch.setattr(authentication, "urlopen", fetch)
    verifier = JWTBearerTokenVerifier(auth_config.bearer)  # type: ignore[arg-type]

    assert verifier.verify(_encode_token(old_key, kid="old-key")).subject
    replacement = _encode_token(replacement_key, kid="replacement-key")
    assert verifier.verify(replacement).subject == "reviewer-bearer"
    assert verifier.verify(replacement).subject == "reviewer-bearer"
    assert calls == 2


def test_remote_jwks_rejects_non_http_url_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_config, private_key, jwks = _remote_bearer_auth_fixture()
    assert auth_config.bearer is not None
    auth_config.bearer.jwks_url = "file:///etc/passwd"
    token = _encode_token(private_key)
    calls = 0

    def fetch(*_args: object, **_kwargs: object) -> _JWKSResponse:
        nonlocal calls
        calls += 1
        return _JWKSResponse(json.dumps(jwks).encode())

    monkeypatch.setattr(authentication, "urlopen", fetch)

    with pytest.raises(AuthenticationError, match="^invalid bearer token$"):
        JWTBearerTokenVerifier(auth_config.bearer).verify(token)

    assert calls == 0


async def test_runs_rejects_expired_bearer_token(sqlite_db) -> None:
    auth_config, private_key = _bearer_auth_fixture()
    bad_token = _encode_token(
        private_key,
        exp=int((datetime.now(UTC) - timedelta(minutes=5)).timestamp()),
    )
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-bearer-expired"),
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=auth_config,
    )

    with TestClient(app) as client:
        response = client.get("/runs", headers=_token_headers(bad_token))

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid bearer token"}


async def test_health_bypasses_auth_even_with_bad_bearer_token(sqlite_db) -> None:
    """Health endpoints should return 200 even when presented with an invalid bearer token."""
    auth_config, private_key = _bearer_auth_fixture()
    bad_token = _encode_token(private_key, iss="https://wrong-issuer.example.test")
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-bearer-wrong-issuer"),
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=auth_config,
    )

    with TestClient(app) as client:
        response = client.get("/health", headers=_token_headers(bad_token))

    assert response.status_code == 200


async def test_runs_rejects_bearer_token_with_wrong_issuer(sqlite_db) -> None:
    auth_config, private_key = _bearer_auth_fixture()
    bad_token = _encode_token(private_key, iss="https://wrong-issuer.example.test")
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-bearer-wrong-iss-runs"),
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=auth_config,
    )

    with TestClient(app) as client:
        response = client.get("/runs", headers=_token_headers(bad_token))

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid bearer token"}


async def test_runs_rejects_bearer_token_with_wrong_audience(sqlite_db) -> None:
    auth_config, private_key = _bearer_auth_fixture()
    bad_token = _encode_token(private_key, aud="wrong-audience")
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-bearer-wrong-audience"),
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=auth_config,
    )

    with TestClient(app) as client:
        response = client.get("/runs", headers=_token_headers(bad_token))

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid bearer token"}


async def test_runs_rejects_bearer_token_with_wrong_signature(sqlite_db) -> None:
    auth_config, _ = _bearer_auth_fixture()
    bad_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad_token = _encode_token(bad_private_key)
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-bearer-wrong-signature"),
        auth_config=auth_config,
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=auth_config,
    )

    with TestClient(app) as client:
        response = client.get("/runs", headers=_token_headers(bad_token))

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid bearer token"}


def _token_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_verified_bearer_claim_can_deliver_platform_admin() -> None:
    auth_config, private_key = _bearer_auth_fixture()
    token = _encode_token(private_key, roles=[ServiceRole.PLATFORM_ADMIN.value])

    principal = JWTBearerTokenVerifier(auth_config.bearer).verify(token)  # type: ignore[arg-type]

    assert principal.roles == [ServiceRole.PLATFORM_ADMIN]


def test_unverified_platform_admin_claim_is_rejected() -> None:
    auth_config, _ = _bearer_auth_fixture()
    untrusted_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _encode_token(untrusted_key, roles=["platform_admin"])

    with pytest.raises(AuthenticationError, match="invalid bearer token"):
        JWTBearerTokenVerifier(auth_config.bearer).verify(token)  # type: ignore[arg-type]


def test_bearer_key_rotation_accepts_replacement_and_rejects_retired_kid() -> None:
    auth_config, replacement_key = _bearer_auth_fixture(kid="replacement-key")
    verifier = JWTBearerTokenVerifier(auth_config.bearer)  # type: ignore[arg-type]

    replacement = _encode_token(replacement_key, kid="replacement-key")
    assert verifier.verify(replacement).subject == "reviewer-bearer"

    retired_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    retired = _encode_token(retired_key, kid="retired-key")
    with pytest.raises(AuthenticationError, match="invalid bearer token"):
        verifier.verify(retired)
