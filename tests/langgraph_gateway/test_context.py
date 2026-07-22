"""Signed reserved context and governed JSON mutation tests."""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import Mapping

import pytest

from zeroth.core.langgraph_gateway.context import (
    GatewayContextError,
    ReservedContextClaims,
    ReservedContextCodec,
    inject_reserved_context,
)
from zeroth.core.signing import EnvHmacSigner, NullSigner


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode_part(part: str) -> object:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _claims(**updates: object) -> ReservedContextClaims:
    values = {
        "tenant_id": "tenant-a",
        "principal_id": "user-7",
        "roles": ("operator", "admin"),
        "deployment_ref": "external-agent",
        "audience": "agent-server:fixture",
        "correlation_id": "corr-1",
        "policy_version": "sha256:abc",
        "issued_at": 100,
        "expires_at": 160,
    }
    values.update(updates)
    return ReservedContextClaims(**values)


@pytest.fixture
def signer() -> EnvHmacSigner:
    return EnvHmacSigner(key_id="gateway-k1", keys={"gateway-k1": b"gateway-secret"})


@pytest.fixture
def codec(signer: EnvHmacSigner) -> ReservedContextCodec:
    return ReservedContextCodec(signer, clock=lambda: 120, max_ttl_seconds=300)


def test_signed_context_round_trip_has_exact_header_and_sorted_roles(
    codec: ReservedContextCodec,
) -> None:
    claims = _claims(roles=("operator", "admin"), content_classification="restricted")

    token = codec.encode(claims)

    assert codec.decode(
        token,
        audience="agent-server:fixture",
        deployment_ref="external-agent",
    ) == _claims(roles=("admin", "operator"), content_classification="restricted")
    header, payload, _ = token.split(".")
    assert _decode_part(header) == {
        "alg": "HS256",
        "kid": "gateway-k1",
        "typ": "ZEROTH-RUN-CONTEXT",
        "v": 1,
    }
    assert _decode_part(payload)["schema_version"] == 1


def test_encoding_is_canonical_and_url_safe_without_padding(
    codec: ReservedContextCodec,
) -> None:
    first = ReservedContextClaims(
        **{
            "tenant_id": "tenant-a",
            "principal_id": "user-7",
            "roles": ("z", "a"),
            "deployment_ref": "external-agent",
            "audience": "agent-server:fixture",
            "correlation_id": "corr-1",
            "policy_version": "sha256:abc",
            "issued_at": 100,
            "expires_at": 160,
        }
    )
    second = ReservedContextClaims(
        **{
            "expires_at": 160,
            "issued_at": 100,
            "policy_version": "sha256:abc",
            "correlation_id": "corr-1",
            "audience": "agent-server:fixture",
            "deployment_ref": "external-agent",
            "roles": ("a", "z"),
            "principal_id": "user-7",
            "tenant_id": "tenant-a",
        }
    )

    assert codec.encode(first) == codec.encode(second)
    assert all("=" not in part for part in codec.encode(first).split("."))
    assert all(
        set(part) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        for part in codec.encode(first).split(".")
    )


def _replace_part(
    token: str,
    index: int,
    value: Mapping[str, object] | bytes,
    signer: EnvHmacSigner | None = None,
) -> str:
    parts = token.split(".")
    raw = (
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        if isinstance(value, Mapping)
        else value
    )
    parts[index] = _b64url(raw)
    if signer is not None:
        parts[2] = _b64url(signer.sign(f"{parts[0]}.{parts[1]}".encode("ascii")))
    return ".".join(parts)


@pytest.mark.parametrize("part", [0, 1, 2])
def test_tampered_token_is_rejected(codec: ReservedContextCodec, part: int) -> None:
    token = codec.encode(_claims())
    replacement = b"altered" if part == 2 else {"attacker": True}

    with pytest.raises(GatewayContextError) as exc_info:
        codec.decode(
            _replace_part(token, part, replacement),
            audience="agent-server:fixture",
            deployment_ref="external-agent",
        )

    assert exc_info.value.code == "zeroth.invalid_context"


@pytest.mark.parametrize(
    ("header_update", "claims_update", "audience", "deployment_ref"),
    [
        ({"alg": "none"}, {}, "agent-server:fixture", "external-agent"),
        ({"typ": "JWT"}, {}, "agent-server:fixture", "external-agent"),
        ({"v": 2}, {}, "agent-server:fixture", "external-agent"),
        ({"v": True}, {}, "agent-server:fixture", "external-agent"),
        ({"kid": "unknown"}, {}, "agent-server:fixture", "external-agent"),
        ({}, {}, "wrong-audience", "external-agent"),
        ({}, {}, "agent-server:fixture", "wrong-deployment"),
        ({}, {"issued_at": 121, "expires_at": 160}, "agent-server:fixture", "external-agent"),
        ({}, {"expires_at": 120}, "agent-server:fixture", "external-agent"),
        ({}, {"issued_at": 160, "expires_at": 160}, "agent-server:fixture", "external-agent"),
        ({}, {"expires_at": 401}, "agent-server:fixture", "external-agent"),
    ],
)
def test_context_validation_rejects_invalid_security_bounds(
    signer: EnvHmacSigner,
    codec: ReservedContextCodec,
    header_update: dict[str, object],
    claims_update: dict[str, object],
    audience: str,
    deployment_ref: str,
) -> None:
    token = codec.encode(_claims(**claims_update))
    if header_update:
        header = _decode_part(token.split(".")[0])
        assert isinstance(header, dict)
        header.update(header_update)
        token = _replace_part(token, 0, header, signer)

    with pytest.raises(GatewayContextError) as exc_info:
        codec.decode(token, audience=audience, deployment_ref=deployment_ref)

    assert exc_info.value.code == "zeroth.invalid_context"


@pytest.mark.parametrize(
    "token", ["", "one", "one.two", "one.two.three.four", "..", "a=.b.c", "***.b.c"]
)
def test_malformed_compact_tokens_are_rejected(codec: ReservedContextCodec, token: str) -> None:
    with pytest.raises(GatewayContextError) as exc_info:
        codec.decode(token, audience="agent-server:fixture", deployment_ref="external-agent")

    assert exc_info.value.code == "zeroth.invalid_context"


def test_noncanonical_json_is_rejected_even_with_valid_signature(
    signer: EnvHmacSigner, codec: ReservedContextCodec
) -> None:
    token = codec.encode(_claims())
    parts = token.split(".")
    header = _decode_part(parts[0])
    parts[0] = _b64url(json.dumps(header, indent=1).encode())
    parts[2] = _b64url(signer.sign(f"{parts[0]}.{parts[1]}".encode("ascii")))

    with pytest.raises(GatewayContextError):
        codec.decode(
            ".".join(parts), audience="agent-server:fixture", deployment_ref="external-agent"
        )


def test_null_and_empty_signers_cannot_mint_context() -> None:
    for signer in (NullSigner(), _VerifyOnlySigner(b"secret")):
        codec = ReservedContextCodec(signer, clock=lambda: 120, max_ttl_seconds=300)
        with pytest.raises(GatewayContextError) as exc_info:
            codec.encode(_claims())
        assert exc_info.value.code == "zeroth.context_signing_unavailable"


class _VerifyOnlySigner:
    def __init__(self, key: bytes) -> None:
        self._verifier = EnvHmacSigner(key_id="gateway-k1", keys={"gateway-k1": key})

    def key_id(self) -> str:
        return "gateway-k1"

    def algorithm(self) -> str:
        return "HS256"

    def sign(self, message: bytes) -> bytes:
        return b""

    def verify(self, message: bytes, signature: bytes, key_id: str) -> bool:
        return self._verifier.verify(message, signature, key_id)


def test_verify_only_provider_can_decode_but_not_encode() -> None:
    issuer = EnvHmacSigner(key_id="gateway-k1", keys={"gateway-k1": b"secret"})
    token = ReservedContextCodec(issuer, clock=lambda: 120).encode(_claims())
    verifier = ReservedContextCodec(_VerifyOnlySigner(b"secret"), clock=lambda: 120)

    assert (
        verifier.decode(token, audience="agent-server:fixture", deployment_ref="external-agent")
        == _claims()
    )


def test_rotated_provider_verifies_token_from_retained_key() -> None:
    old = EnvHmacSigner(key_id="old", keys={"old": b"old-secret"})
    token = ReservedContextCodec(old, clock=lambda: 120).encode(_claims())
    rotated = EnvHmacSigner(key_id="new", keys={"old": b"old-secret", "new": b"new-secret"})

    assert (
        ReservedContextCodec(rotated, clock=lambda: 120).decode(
            token, audience="agent-server:fixture", deployment_ref="external-agent"
        )
        == _claims()
    )


def _spoofed_body() -> dict[str, object]:
    return {
        "_zeroth": "top-spoof",
        "input": {"messages": [{"role": "user", "content": "hello"}]},
        "context": {"_zeroth": "context-spoof", "keep": 1},
        "metadata": {"_zeroth": "metadata-spoof", "trace": {"sample": True}},
        "config": {
            "tags": ["existing"],
            "callbacks": [{"name": "json-callback", "args": [1, {"x": 2}]}],
            "configurable": {"_zeroth": "config-spoof", "thread_ts": 42},
        },
        "unknown": [1, {"nested": "value"}],
    }


@pytest.mark.parametrize("shape", ["threaded", "stateless", "protocol-v2"])
def test_injection_removes_all_spoofs_and_preserves_unknown_json(shape: str) -> None:
    target = _spoofed_body()
    source = (
        target
        if shape != "protocol-v2"
        else {
            "id": "cmd-1",
            "method": "run.start",
            "params": target,
            "other": {"keep": True},
            "_zeroth": "envelope-spoof",
        }
    )
    original = copy.deepcopy(source)

    encoded = inject_reserved_context(
        source,
        "gateway.token.value",
        max_body_bytes=10_000,
        protocol_v2=shape == "protocol-v2",
    )
    result = json.loads(encoded)
    mutated = result["params"] if shape == "protocol-v2" else result

    assert source == original
    assert "_zeroth" not in mutated
    assert "_zeroth" not in mutated["context"]
    assert "_zeroth" not in mutated["metadata"]
    assert mutated["config"]["configurable"] == {
        "thread_ts": 42,
        "_zeroth": "gateway.token.value",
    }
    assert mutated["config"]["tags"] == ["existing"]
    original_target = original["params"] if shape == "protocol-v2" else original
    assert mutated["config"]["callbacks"] == original_target["config"]["callbacks"]
    assert mutated["unknown"] == [1, {"nested": "value"}]
    assert encoded == json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    assert encoded.count(b"_zeroth") == 1
    if shape == "protocol-v2":
        assert "_zeroth" not in result
        assert result["id"] == "cmd-1"
        assert result["method"] == "run.start"
        assert result["other"] == {"keep": True}


@pytest.mark.parametrize("source", [{}, {"config": {}}, {"config": {"configurable": {}}}])
def test_injection_creates_missing_config_path(source: dict[str, object]) -> None:
    result = json.loads(inject_reserved_context(source, "signed", max_body_bytes=1000))

    assert result["config"]["configurable"]["_zeroth"] == "signed"


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (b"not-json", "zeroth.invalid_request"),
        (b"[]", "zeroth.invalid_request"),
        (b"null", "zeroth.invalid_request"),
        (b'{"config":[]}', "zeroth.invalid_request"),
        (b'{"method":"run.start","params":[]}', "zeroth.invalid_request"),
    ],
)
def test_invalid_or_non_object_json_has_safe_error_code(body: bytes, expected_code: str) -> None:
    with pytest.raises(GatewayContextError) as exc_info:
        inject_reserved_context(
            body,
            "signed",
            max_body_bytes=1000,
            protocol_v2=b"params" in body,
        )

    assert exc_info.value.code == expected_code
    assert body.decode(errors="ignore") not in str(exc_info.value)


def test_oversize_body_is_rejected_before_json_parse() -> None:
    body = b"{" + b"x" * 20

    with pytest.raises(GatewayContextError) as exc_info:
        inject_reserved_context(body, "signed", max_body_bytes=10)

    assert exc_info.value.code == "zeroth.request_too_large"


def test_python_callback_objects_are_rejected_without_being_copied() -> None:
    callback = object()
    source = {"config": {"callbacks": [callback]}}

    with pytest.raises(GatewayContextError) as exc_info:
        inject_reserved_context(source, "signed", max_body_bytes=1000)

    assert exc_info.value.code == "zeroth.invalid_request"
    assert source["config"]["callbacks"][0] is callback


@pytest.mark.parametrize(
    "source",
    [
        {"config": {"tags": ("python-tuple",)}},
        {1: "non-string object key"},
    ],
)
def test_mapping_input_rejects_values_that_are_not_json(source: dict[object, object]) -> None:
    with pytest.raises(GatewayContextError) as exc_info:
        inject_reserved_context(source, "signed", max_body_bytes=1000)

    assert exc_info.value.code == "zeroth.invalid_request"
