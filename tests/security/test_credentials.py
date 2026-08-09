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


@pytest.mark.parametrize("identifiers", ["deploy-key", b"deploy-key", [""], [1]])
def test_registry_rejects_ambiguous_or_invalid_identifier_collections(identifiers: object) -> None:
    with pytest.raises(ValueError):
        CredentialRevocationRegistry(identifiers)  # type: ignore[arg-type]


@pytest.mark.parametrize("identifiers", ["deploy-key", b"deploy-key", [""], [1]])
def test_registry_snapshot_replacement_rejects_ambiguous_or_invalid_identifiers(
    identifiers: object,
) -> None:
    registry = CredentialRevocationRegistry()

    with pytest.raises(ValueError):
        registry.replace_snapshot(identifiers)  # type: ignore[arg-type]


def test_snapshot_replacement_waits_for_an_inflight_old_snapshot_decision() -> None:
    decision_started = threading.Event()
    release_decision = threading.Event()
    replacement_started = threading.Event()
    replacement_finished = threading.Event()

    class PausingRegistry(CredentialRevocationRegistry):
        def is_revoked(self, identifier: str) -> bool:
            decision_started.set()
            assert release_decision.wait(timeout=1)
            return super().is_revoked(identifier)

    registry = PausingRegistry()
    authenticator = ServiceAuthenticator(_static_config(), credential_status_provider=registry)

    auth_thread = threading.Thread(
        target=lambda: authenticator.authenticate_headers({"X-API-Key": "once-valid-secret"})
    )

    def replace() -> None:
        replacement_started.set()
        registry.replace_snapshot({"deploy-key"})
        replacement_finished.set()

    replacement_thread = threading.Thread(target=replace)
    auth_thread.start()
    assert decision_started.wait(timeout=1)
    replacement_thread.start()
    assert replacement_started.wait(timeout=1)
    assert not replacement_finished.is_set()
    release_decision.set()
    auth_thread.join(timeout=1)
    replacement_thread.join(timeout=1)
    assert not auth_thread.is_alive()
    assert replacement_finished.is_set()

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


@pytest.mark.parametrize("field", ["credential_id", "secret"])
def test_static_api_key_credentials_must_be_unambiguous(field: str) -> None:
    credentials = [
        {
            "credential_id": "earlier-id",
            "secret": "earlier-secret",
            "subject": "earlier",
            "roles": [ServiceRole.OPERATOR],
        },
        {
            "credential_id": "later-revoked-id",
            "secret": "later-secret",
            "subject": "later",
            "roles": [ServiceRole.OPERATOR],
        },
    ]
    credentials[1][field] = credentials[0][field]

    with pytest.raises(ValidationError) as excinfo:
        ServiceAuthConfig(api_keys=credentials)

    assert "earlier-secret" not in str(excinfo.value)
    assert "later-secret" not in str(excinfo.value)


def test_later_revoked_id_cannot_be_hidden_behind_a_duplicate_static_secret() -> None:
    with pytest.raises(ValidationError):
        ServiceAuthConfig(
            api_keys=[
                StaticApiKeyCredential(
                    credential_id="earlier-id",
                    secret="shared-secret",
                    subject="earlier",
                    roles=[ServiceRole.OPERATOR],
                ),
                StaticApiKeyCredential(
                    credential_id="later-revoked-id",
                    secret="shared-secret",
                    subject="later",
                    roles=[ServiceRole.OPERATOR],
                ),
            ],
            revoked_credential_ids=frozenset({"later-revoked-id"}),
        )


def test_duplicate_secret_validation_redacts_structured_diagnostics_and_keeps_valid_secret_usable() -> None:
    sentinel = "SENTINEL_RAW_STATIC_SECRET"
    duplicate_config = {
        "api_keys": [
            {
                "credential_id": "first",
                "secret": sentinel,
                "subject": "first",
                "roles": [ServiceRole.OPERATOR],
            },
            {
                "credential_id": "second",
                "secret": sentinel,
                "subject": "second",
                "roles": [ServiceRole.OPERATOR],
            },
        ]
    }

    loaders = (
        lambda: ServiceAuthConfig(**duplicate_config),
        lambda: ServiceAuthConfig.model_validate(duplicate_config),
        lambda: ServiceAuthConfig.from_env(
            {"ZEROTH_SERVICE_API_KEYS_JSON": json.dumps(duplicate_config["api_keys"])}
        ),
    )
    for load in loaders:
        with pytest.raises(ValidationError) as excinfo:
            load()
        error = excinfo.value
        for diagnostic in (str(error), repr(error), error.errors(), error.json()):
            assert sentinel not in str(diagnostic)

    valid = ServiceAuthenticator(
        ServiceAuthConfig(
            api_keys=[
                StaticApiKeyCredential(
                    credential_id="usable",
                    secret=sentinel,
                    subject="usable-subject",
                    roles=[ServiceRole.OPERATOR],
                )
            ]
        )
    )
    assert valid.authenticate_headers({"X-API-Key": sentinel}).subject == "usable-subject"


def _assert_secret_absent_from_exception_chain(error: BaseException, sentinel: str) -> None:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        diagnostics: list[object] = [str(current), repr(current)]
        if isinstance(current, ValidationError):
            diagnostics.extend((current.errors(), current.json()))
        for diagnostic in diagnostics:
            assert sentinel not in str(diagnostic)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)


@pytest.mark.parametrize(
    "api_keys",
    [
        lambda sentinel: [
            {
                "credential_id": "first",
                "secret": sentinel,
                "subject": "first",
                "roles": [ServiceRole.OPERATOR],
            },
            {
                "credential_id": "second",
                "secret": sentinel,
                "subject": "second",
                "roles": [ServiceRole.OPERATOR],
            },
        ],
        lambda sentinel: [
            {
                "credential_id": "malformed",
                "secret": {"value": sentinel},
                "subject": "subject",
                "roles": [ServiceRole.OPERATOR],
            }
        ],
        lambda sentinel: [
            {
                "credential_id": "extra",
                "secret": "valid-secret",
                "client_secret": sentinel,
                "subject": "subject",
                "roles": [ServiceRole.OPERATOR],
            }
        ],
        lambda sentinel: [
            {
                "credential_id": "role-secret",
                "secret": "valid-secret",
                "subject": "subject",
                "roles": [{"secret": sentinel}],
            }
        ],
        lambda sentinel: [
            {
                "credential_id": "hostile-extra",
                "secret": "valid-secret",
                "subject": "subject",
                "roles": [ServiceRole.OPERATOR],
                f"{sentinel}_client_secret": "ignored",
            }
        ],
    ],
)
def test_config_validation_never_retains_sensitive_input_in_exception_chain(api_keys) -> None:
    sentinel = "SENTINEL_RAW_CONFIG_SECRET"
    parsed_api_keys = api_keys(sentinel)
    loaders = (
        lambda: ServiceAuthConfig(api_keys=parsed_api_keys),
        lambda: ServiceAuthConfig.model_validate({"api_keys": parsed_api_keys}),
        lambda: ServiceAuthConfig.from_env(
            {"ZEROTH_SERVICE_API_KEYS_JSON": json.dumps(parsed_api_keys)}
        ),
    )

    for load in loaders:
        with pytest.raises(ValidationError) as excinfo:
            load()
        assert excinfo.value.__context__ is None
        assert excinfo.value.__cause__ is None
        _assert_secret_absent_from_exception_chain(excinfo.value, sentinel)


def test_invalid_role_enum_is_sanitized_without_losing_enum_guidance() -> None:
    api_keys = [
        {
            "credential_id": "invalid-role",
            "secret": "valid-secret",
            "subject": "subject",
            "roles": [""],
        }
    ]
    loaders = (
        lambda: ServiceAuthConfig(api_keys=api_keys),
        lambda: ServiceAuthConfig.model_validate({"api_keys": api_keys}),
        lambda: ServiceAuthConfig.from_env({"ZEROTH_SERVICE_API_KEYS_JSON": json.dumps(api_keys)}),
    )

    for load in loaders:
        with pytest.raises(ValidationError) as excinfo:
            load()
        error = excinfo.value
        assert error.__context__ is None
        assert error.__cause__ is None
        detail = error.errors()[0]
        assert detail["type"] == "enum"
        assert detail["loc"] == ("api_keys", 0, "roles", 0)
        assert "operator" in detail["msg"]


def _config_environment(payload: dict[str, object]) -> dict[str, str]:
    """Serialize a complete auth config payload through its public env API."""
    env_names = {
        "api_keys": "ZEROTH_SERVICE_API_KEYS_JSON",
        "bearer": "ZEROTH_SERVICE_BEARER_JSON",
        "custom_roles": "ZEROTH_SERVICE_ROLES_JSON",
        "revoked_credential_ids": "ZEROTH_SERVICE_REVOKED_CREDENTIAL_IDS_JSON",
    }
    return {env_names[key]: json.dumps(value) for key, value in payload.items()}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "api_keys": [
                    {
                        "credential_id": "",
                        "secret": "valid-secret",
                        "subject": "subject",
                    }
                ]
            },
            "credential_id must be non-empty",
        ),
        (
            {
                "api_keys": [
                    {"credential_id": "same", "secret": "first", "subject": "first"},
                    {"credential_id": "same", "secret": "second", "subject": "second"},
                ]
            },
            "static API key credential IDs must be unique",
        ),
        (
            {
                "api_keys": [
                    {"credential_id": "first", "secret": "same", "subject": "first"},
                    {"credential_id": "second", "secret": "same", "subject": "second"},
                ]
            },
            "static API key secrets must be unique",
        ),
        (
            {"revoked_credential_ids": "not-an-array"},
            "revoked credential identifiers must be an array of strings",
        ),
        (
            {"revoked_credential_ids": ["same", "same"]},
            "revoked credential identifiers must be unique",
        ),
        (
            {"bearer": {"issuer": "issuer", "audience": "audience"}},
            "bearer auth requires jwks_url or jwks",
        ),
    ],
)
def test_config_validation_preserves_distinct_safe_messages(
    payload: dict[str, object], message: str
) -> None:
    loaders = (
        lambda: ServiceAuthConfig(**payload),
        lambda: ServiceAuthConfig.model_validate(payload),
        lambda: ServiceAuthConfig.from_env(_config_environment(payload)),
    )

    for load in loaders:
        with pytest.raises(ValidationError) as excinfo:
            load()

        assert message in str(excinfo.value)


@pytest.mark.parametrize("credential_id", ["", "   "])
def test_service_auth_config_rejects_empty_or_whitespace_static_credential_ids(
    credential_id: str,
) -> None:
    with pytest.raises(ValidationError):
        ServiceAuthConfig(
            api_keys=[
                {
                    "credential_id": credential_id,
                    "secret": "valid-secret",
                    "subject": "subject",
                    "roles": [ServiceRole.OPERATOR],
                }
            ]
        )


@pytest.mark.parametrize("identifier", ["", "   "])
def test_service_auth_config_rejects_empty_or_whitespace_revocation_ids(identifier: str) -> None:
    with pytest.raises(ValidationError):
        ServiceAuthConfig(revoked_credential_ids=[identifier])


def test_whitespace_jti_uses_the_verified_token_fingerprint_for_revocation() -> None:
    config, private_key = _bearer_config()
    token = _token(private_key, jti="   ")
    fingerprint = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
    authenticator = ServiceAuthenticator(
        config.model_copy(update={"revoked_credential_ids": frozenset({fingerprint})})
    )

    with pytest.raises(AuthenticationError, match="^authentication required$"):
        _authenticate(authenticator, token)


@pytest.mark.parametrize("payload", ['not-json', '["duplicate", "duplicate"]', '["valid", 2]'])
def test_revocation_environment_config_rejects_malformed_or_ambiguous_identifiers(payload: str) -> None:
    with pytest.raises((ValidationError, ValueError, json.JSONDecodeError)):
        ServiceAuthConfig.from_env({"ZEROTH_SERVICE_REVOKED_CREDENTIAL_IDS_JSON": payload})
