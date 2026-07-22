"""Signed reserved run context and governed JSON request mutation."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator

from zeroth.core.signing import SigningKeyProvider

_CONTEXT_TYPE = "ZEROTH-RUN-CONTEXT"
_SCHEMA_VERSION = 1
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_INVALID_CONTEXT = "zeroth.invalid_context"
_INVALID_REQUEST = "zeroth.invalid_request"
_REQUEST_TOO_LARGE = "zeroth.request_too_large"
_SIGNING_UNAVAILABLE = "zeroth.context_signing_unavailable"
_DEFAULT_MAX_TOKEN_CHARS = 8192
_DEFAULT_MAX_HEADER_BYTES = 1024
_DEFAULT_MAX_PAYLOAD_BYTES = 4096
_DEFAULT_MAX_SIGNATURE_BYTES = 512


class GatewayContextError(ValueError):
    """Safe typed failure raised by context verification or body mutation."""

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason)


class ReservedContextClaims(BaseModel):
    """Versioned claims trusted by an upstream Agent Server deployment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = _SCHEMA_VERSION
    tenant_id: StrictStr
    principal_id: StrictStr
    roles: tuple[StrictStr, ...]
    deployment_ref: StrictStr
    audience: StrictStr
    correlation_id: StrictStr
    policy_version: StrictStr
    issued_at: StrictInt
    expires_at: StrictInt
    content_classification: StrictStr | None = None

    @field_validator("roles", mode="after")
    @classmethod
    def sort_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Make role ordering deterministic before serialization and comparison."""
        return tuple(sorted(value))


class ReservedContextCodec:
    """Mint and verify compact, canonical, signed reserved context tokens."""

    def __init__(
        self,
        signer: SigningKeyProvider,
        *,
        clock: Callable[[], int | float] = time.time,
        max_ttl_seconds: int = 300,
        max_token_chars: int = _DEFAULT_MAX_TOKEN_CHARS,
        max_header_bytes: int = _DEFAULT_MAX_HEADER_BYTES,
        max_payload_bytes: int = _DEFAULT_MAX_PAYLOAD_BYTES,
        max_signature_bytes: int = _DEFAULT_MAX_SIGNATURE_BYTES,
    ) -> None:
        limits = (
            max_ttl_seconds,
            max_token_chars,
            max_header_bytes,
            max_payload_bytes,
            max_signature_bytes,
        )
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("context limits must be positive integers")
        self._signer = signer
        self._clock = clock
        self._max_ttl_seconds = max_ttl_seconds
        self._max_token_chars = max_token_chars
        self._max_header_bytes = max_header_bytes
        self._max_payload_bytes = max_payload_bytes
        self._max_signature_bytes = max_signature_bytes

    def encode(self, claims: ReservedContextClaims) -> str:
        """Return a canonical compact token, failing closed without a signature."""
        try:
            algorithm = self._signer.algorithm()
            key_id = self._signer.key_id()
            if type(algorithm) is not str or not algorithm:
                raise ValueError("invalid signing algorithm")
            if type(key_id) is not str or not key_id:
                raise ValueError("invalid signing key id")
            header = {
                "alg": algorithm,
                "kid": key_id,
                "typ": _CONTEXT_TYPE,
                "v": _SCHEMA_VERSION,
            }
            header_part = _encode_bounded_segment(_canonical_json(header), self._max_header_bytes)
            payload_part = _encode_bounded_segment(
                _canonical_json(claims.model_dump(mode="json", exclude_none=True)),
                self._max_payload_bytes,
            )
            signing_input = f"{header_part}.{payload_part}".encode("ascii")
            signature = self._signer.sign(signing_input)
            if type(signature) is not bytes or not signature:
                raise ValueError("invalid signature")
            signature_part = _encode_bounded_segment(signature, self._max_signature_bytes)
            token = f"{header_part}.{payload_part}.{signature_part}"
            if len(token) > self._max_token_chars:
                raise ValueError("context token exceeds maximum size")
            return token
        except Exception:
            raise GatewayContextError(
                _SIGNING_UNAVAILABLE, "reserved context signing is unavailable"
            ) from None

    def decode(
        self,
        token: str,
        *,
        audience: str,
        deployment_ref: str,
    ) -> ReservedContextClaims:
        """Verify a token and its deployment, audience, and lifetime bounds."""
        try:
            if type(token) is not str or len(token) > self._max_token_chars:
                raise ValueError("invalid compact token size")
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError("wrong segment count")
            header_part, payload_part, signature_part = parts
            _validate_segment_encoding(header_part, self._max_header_bytes)
            _validate_segment_encoding(payload_part, self._max_payload_bytes)
            _validate_segment_encoding(signature_part, self._max_signature_bytes)

            header = _decode_json_object(header_part, self._max_header_bytes)
            signature = _decode_segment(signature_part, self._max_signature_bytes)
            if not signature:
                raise ValueError("empty signature")

            try:
                expected_algorithm = self._signer.algorithm()
                if type(expected_algorithm) is not str or not expected_algorithm:
                    raise ValueError("invalid signing algorithm")
            except Exception:
                raise ValueError("signing algorithm unavailable") from None
            if (
                set(header) != {"alg", "kid", "typ", "v"}
                or header["alg"] != expected_algorithm
                or not isinstance(header["kid"], str)
                or not header["kid"]
                or header["typ"] != _CONTEXT_TYPE
                or type(header["v"]) is not int
                or header["v"] != _SCHEMA_VERSION
            ):
                raise ValueError("invalid header")

            signing_input = f"{header_part}.{payload_part}".encode("ascii")
            try:
                verification_result = self._signer.verify(signing_input, signature, header["kid"])
            except Exception:
                raise ValueError("signature verification failed") from None
            if verification_result is not True:
                raise ValueError("invalid signature")

            payload = _decode_json_object(payload_part, self._max_payload_bytes)
            claims = ReservedContextClaims.model_validate(payload)
            try:
                now = self._clock()
            except Exception:
                raise ValueError("clock unavailable") from None
            if type(now) is not int and (type(now) is not float or not math.isfinite(now)):
                raise ValueError("invalid clock value")
            if claims.audience != audience or claims.deployment_ref != deployment_ref:
                raise ValueError("context target mismatch")
            if claims.issued_at > now:
                raise ValueError("context issued in the future")
            if claims.expires_at <= now:
                raise ValueError("context expired")
            if claims.expires_at <= claims.issued_at:
                raise ValueError("invalid context lifetime")
            if claims.expires_at - claims.issued_at > self._max_ttl_seconds:
                raise ValueError("context lifetime exceeds maximum")
        except GatewayContextError:
            raise
        except Exception:
            raise GatewayContextError(_INVALID_CONTEXT, "reserved context is invalid") from None
        return claims


def inject_reserved_context(
    body: bytes | bytearray | memoryview | Mapping[str, object],
    token: str,
    *,
    max_body_bytes: int,
    protocol_v2: bool = False,
) -> bytes:
    """Return deterministic JSON with one gateway-owned reserved context token.

    Byte inputs are size-checked before any decoding or JSON parsing. Mapping
    inputs are first serialized through strict JSON, which both clones them and
    rejects callback objects or other process-local Python values.
    """
    if max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be positive")

    raw = _body_bytes(body)
    if len(raw) > max_body_bytes:
        raise GatewayContextError(_REQUEST_TOO_LARGE, "governed request body is too large")

    try:
        value = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-JSON number")),
        )
        if not isinstance(value, dict):
            raise ValueError("request must be an object")
        value.pop("_zeroth", None)
        target = value.get("params") if protocol_v2 else value
        if not isinstance(target, dict):
            raise ValueError("governed request target must be an object")
        _remove_spoofed_context(target)
        configurable = _configurable_object(target)
        configurable["_zeroth"] = token
        return _canonical_json(value)
    except GatewayContextError:
        raise
    except Exception as exc:
        raise GatewayContextError(_INVALID_REQUEST, "governed request is invalid") from exc


def _body_bytes(body: bytes | bytearray | memoryview | Mapping[str, object]) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, (bytearray, memoryview)):
        return bytes(body)
    if isinstance(body, Mapping):
        try:
            return _canonical_json(_clone_json_value(body))
        except Exception as exc:
            raise GatewayContextError(_INVALID_REQUEST, "governed request is invalid") from exc
    raise GatewayContextError(_INVALID_REQUEST, "governed request is invalid")


def _remove_spoofed_context(target: dict[str, object]) -> None:
    target.pop("_zeroth", None)
    for key in ("context", "metadata"):
        nested = target.get(key)
        if isinstance(nested, dict):
            nested.pop("_zeroth", None)

    config = target.get("config")
    if isinstance(config, dict):
        configurable = config.get("configurable")
        if isinstance(configurable, dict):
            configurable.pop("_zeroth", None)


def _configurable_object(target: dict[str, object]) -> dict[str, object]:
    config = target.get("config")
    if config is None:
        config = {}
        target["config"] = config
    if not isinstance(config, dict):
        raise ValueError("config must be an object")

    configurable = config.get("configurable")
    if configurable is None:
        configurable = {}
        config["configurable"] = configurable
    if not isinstance(configurable, dict):
        raise ValueError("config.configurable must be an object")
    return configurable


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _clone_json_value(value: object) -> object:
    """Clone only values that exist in the JSON data model."""
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return value
    if type(value) is list:
        return [_clone_json_value(item) for item in value]
    if isinstance(value, Mapping):
        if not all(type(key) is str for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: _clone_json_value(item) for key, item in value.items()}
    raise TypeError("value is outside the JSON data model")


def _encode_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _encode_bounded_segment(value: bytes, max_bytes: int) -> str:
    if len(value) > max_bytes:
        raise ValueError("context segment exceeds maximum size")
    encoded = _encode_segment(value)
    _validate_segment_encoding(encoded, max_bytes)
    return encoded


def _validate_segment_encoding(value: str, max_bytes: int) -> None:
    max_chars = (max_bytes * 4 + 2) // 3
    if (
        not value
        or len(value) > max_chars
        or not _BASE64URL.fullmatch(value)
        or len(value) % 4 == 1
    ):
        raise ValueError("invalid compact segment")


def _decode_segment(value: str, max_bytes: int) -> bytes:
    _validate_segment_encoding(value, max_bytes)
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid compact segment") from exc
    if len(decoded) > max_bytes or _encode_segment(decoded) != value:
        raise ValueError("noncanonical compact segment")
    return decoded


def _decode_json_object(value: str, max_bytes: int) -> dict[str, object]:
    raw = _decode_segment(value, max_bytes)
    decoded = json.loads(
        raw,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-JSON number")),
    )
    if not isinstance(decoded, dict) or _canonical_json(decoded) != raw:
        raise ValueError("noncanonical JSON object")
    return decoded


__all__ = [
    "GatewayContextError",
    "ReservedContextClaims",
    "ReservedContextCodec",
    "inject_reserved_context",
]
