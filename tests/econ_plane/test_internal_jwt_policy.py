"""Preserve internal token policy while removing the fallback JWT dependency."""

import time

import jwt
import pytest

from zeroth.econ.plane.auth import service


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(service.settings, "jwt_secret", "internal-policy-test-key-" * 3)
    monkeypatch.setattr(service.settings, "jwt_algorithm", "HS256")

    def encode(**claims):
        payload = dict(
            sub="fixture",
            email="fixture@example.com",
            roles=["Admin"],
            tenant_id="tenant-a",
            iss="econ-plane",
            exp=int(time.time()) + 600,
        )
        payload.update(claims)
        return jwt.encode(payload, service.settings.jwt_secret, algorithm="HS256")

    return encode


@pytest.mark.parametrize("claim", ["aud", "at_hash"])
@pytest.mark.parametrize("value", [None, "", [], {}, "bound"])
def test_internal_tokens_reject_unsupported_binding_claims(token, claim, value):
    with pytest.raises(jwt.InvalidTokenError):
        service.decode_token(token(**{claim: value}))


@pytest.mark.parametrize("value", [None, "", [], {}, "not-a-number"])
def test_internal_tokens_reject_invalid_issued_at(token, value):
    with pytest.raises(jwt.InvalidTokenError):
        service.decode_token(token(iat=value))


def test_internal_tokens_preserve_future_numeric_issued_at_and_scope(token):
    claims = service.decode_token(token(iat=int(time.time()) + 3600))
    assert claims.tenant_id == "tenant-a"
    assert claims.roles == ["Admin"]


def test_internal_tokens_still_reject_expired_and_tampered_tokens(token):
    with pytest.raises(jwt.InvalidTokenError):
        service.decode_token(token(exp=int(time.time()) - 60))
    encoded = token()
    header, payload, signature = encoded.split(".")
    tampered = f"{header}.{payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    with pytest.raises(jwt.InvalidTokenError):
        service.decode_token(tampered)
