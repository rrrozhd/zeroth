from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tests.service.helpers import approval_resume_graph, deploy_service
from zeroth.identity import ServiceRole
from zeroth.service.auth import BearerTokenConfig, ServiceAuthConfig
from zeroth.service.bootstrap import bootstrap_app


def _bearer_auth_fixture() -> tuple[ServiceAuthConfig, object]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    jwk["kid"] = "test-key"
    config = ServiceAuthConfig(
        bearer=BearerTokenConfig(
            issuer="https://issuer.example.test",
            audience="zeroth-service",
            jwks={"keys": [jwk]},
        )
    )
    return config, private_key


def _encode_token(private_key, **claims: object) -> str:
    payload = {
        "sub": "reviewer-bearer",
        "roles": [ServiceRole.REVIEWER.value],
        "tenant_id": "default",
        "iss": "https://issuer.example.test",
        "aud": "zeroth-service",
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        **claims,
    }
    return jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
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
