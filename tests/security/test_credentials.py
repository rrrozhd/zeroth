"""Regression gates for revocable service credentials."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from zeroth.governance.identity import ServiceRole
from zeroth.service.api.authentication import (
    AuthenticationError,
    BearerTokenConfig,
    CredentialRevocationRegistry,
    ServiceAuthConfig,
    ServiceAuthenticator,
    StaticApiKeyCredential,
)


def _static_config(*, revoked: frozenset[str] = frozenset()) -> ServiceAuthConfig:
    return ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="deploy-key",
                secret="once-valid-secret",
                subject="operator",
                roles=[ServiceRole.OPERATOR],
            )
        ],
        revoked_credential_ids=revoked,
    )


def _bearer_config() -> tuple[ServiceAuthConfig, object]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = "revocation-test-key"
    return (
        ServiceAuthConfig(
            bearer=BearerTokenConfig(
                issuer="https://issuer.example.test",
                audience="zeroth-service",
                jwks={"keys": [jwk]},
            )
        ),
        private_key,
    )


def _token(private_key: object, **claims: object) -> str:
    return jwt.encode(
        {
            "sub": "bearer-user",
            "roles": [ServiceRole.OPERATOR.value],
            "iss": "https://issuer.example.test",
            "aud": "zeroth-service",
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            **claims,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "revocation-test-key"},
    )


def _authenticate(authenticator: ServiceAuthenticator, token: str) -> None:
    authenticator.authenticate_headers({"Authorization": f"Bearer {token}"})


def test_static_key_is_rejected_immediately_after_registry_snapshot_replacement() -> None:
    registry = CredentialRevocationRegistry()
    authenticator = ServiceAuthenticator(_static_config(), credential_status_provider=registry)

    assert authenticator.authenticate_headers({"X-API-Key": "once-valid-secret"}).subject == "operator"
    registry.replace_snapshot({"deploy-key"})

    with pytest.raises(AuthenticationError, match="^authentication required$"):
        authenticator.authenticate_headers({"X-API-Key": "once-valid-secret"})


def test_signed_jwt_jti_and_fingerprint_identifiers_are_revocable_without_storing_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config, private_key = _bearer_config()
    with_jti = _token(private_key, jti="session-42")
    without_jti = _token(private_key)
    fingerprint = "sha256:" + hashlib.sha256(without_jti.encode()).hexdigest()
    registry = CredentialRevocationRegistry({"session-42", fingerprint})
    authenticator = ServiceAuthenticator(config, credential_status_provider=registry)

    for token in (with_jti, without_jti):
        for _ in range(2):  # A captured token remains unusable on replay.
            with pytest.raises(AuthenticationError, match="^authentication required$") as excinfo:
                _authenticate(authenticator, token)
            assert token not in str(excinfo.value)
            assert token not in repr(registry)
            assert token not in registry.snapshot
            assert token not in caplog.text


def test_fingerprint_is_deterministic_and_separates_distinct_signed_tokens() -> None:
    config, private_key = _bearer_config()
    first = _token(private_key, nonce="one")
    second = _token(private_key, nonce="two")
    first_id = "sha256:" + hashlib.sha256(first.encode()).hexdigest()
    registry = CredentialRevocationRegistry({first_id})
    authenticator = ServiceAuthenticator(config, credential_status_provider=registry)

    assert first_id == "sha256:" + hashlib.sha256(first.encode()).hexdigest()
    assert first_id != "sha256:" + hashlib.sha256(second.encode()).hexdigest()
    with pytest.raises(AuthenticationError, match="^authentication required$"):
        _authenticate(authenticator, first)
    _authenticate(authenticator, second)


def test_invalid_credentials_do_not_query_revocation_provider() -> None:
    class Provider:
        calls = 0

        def is_revoked(self, identifier: str) -> bool:
            self.calls += 1
            return False

    provider = Provider()
    authenticator = ServiceAuthenticator(_static_config(), credential_status_provider=provider)

    with pytest.raises(AuthenticationError, match="^authentication required$"):
        authenticator.authenticate_headers({"X-API-Key": "not-a-real-secret"})

    assert provider.calls == 0


def test_falsey_revocation_provider_is_injected_and_honored() -> None:
    class FalseyProvider:
        def __bool__(self) -> bool:
            return False

        def is_revoked(self, identifier: str) -> bool:
            return identifier == "deploy-key"

    authenticator = ServiceAuthenticator(
        _static_config(), credential_status_provider=FalseyProvider()
    )

    with pytest.raises(AuthenticationError, match="^authentication required$"):
        authenticator.authenticate_headers({"X-API-Key": "once-valid-secret"})


def test_registry_snapshot_replacement_is_a_deterministic_revocation_barrier() -> None:
    registry = CredentialRevocationRegistry()
    authenticator = ServiceAuthenticator(_static_config(), credential_status_provider=registry)
    replacement_done = threading.Event()

    def authenticate_after_update() -> bool:
        assert replacement_done.wait(timeout=1)
        with pytest.raises(AuthenticationError, match="^authentication required$"):
            authenticator.authenticate_headers({"X-API-Key": "once-valid-secret"})
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(authenticate_after_update) for _ in range(8)]
        registry.replace_snapshot({"deploy-key"})
        replacement_done.set()
        assert all(future.result(timeout=1) for future in futures)


def test_revocations_round_trip_through_environment_config() -> None:
    environment = {
        "ZEROTH_SERVICE_API_KEYS_JSON": json.dumps(
            [
                {
                    "credential_id": "deploy-key",
                    "secret": "once-valid-secret",
                    "subject": "operator",
                    "roles": [ServiceRole.OPERATOR.value],
                }
            ]
        ),
        "ZEROTH_SERVICE_REVOKED_CREDENTIAL_IDS_JSON": '["deploy-key"]',
    }
    authenticator = ServiceAuthenticator(ServiceAuthConfig.from_env(environment))

    with pytest.raises(AuthenticationError, match="^authentication required$"):
        authenticator.authenticate_headers({"X-API-Key": "once-valid-secret"})


@pytest.mark.parametrize("payload", ['not-json', '["duplicate", "duplicate"]', '["valid", 2]'])
def test_revocation_environment_config_rejects_malformed_or_ambiguous_identifiers(payload: str) -> None:
    with pytest.raises((ValidationError, ValueError, json.JSONDecodeError)):
        ServiceAuthConfig.from_env({"ZEROTH_SERVICE_REVOKED_CREDENTIAL_IDS_JSON": payload})
