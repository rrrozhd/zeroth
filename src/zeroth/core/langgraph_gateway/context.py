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
    ) -> None:
        if max_ttl_seconds <= 0:
            raise ValueError("max_ttl_seconds must be positive")
        self._signer = signer
        self._clock = clock
        self._max_ttl_seconds = max_ttl_seconds

    def encode(self, claims: ReservedContextClaims) -> str:
        """Return a canonical compact token, failing closed without a signature."""
        header = {
            "alg": self._signer.algorithm(),
            "kid": self._signer.key_id(),
            "typ": _CONTEXT_TYPE,
            "v": _SCHEMA_VERSION,
        }
        header_part = _encode_segment(_canonical_json(header))
        payload_part = _encode_segment(
            _canonical_json(claims.model_dump(mode="json", exclude_none=True))
        )
        signing_input = f"{header_part}.{payload_part}".encode("ascii")
        try:
            signature = self._signer.sign(signing_input)
        except Exception as exc:
            raise GatewayContextError(
                _SIGNING_UNAVAILABLE, "reserved context signing is unavailable"
            ) from exc
        if not signature:
            raise GatewayContextError(
                _SIGNING_UNAVAILABLE, "reserved context signing is unavailable"
            )
        return f"{header_part}.{payload_part}.{_encode_segment(signature)}"

    def decode(
        self,
        token: str,
        *,
        audience: str,
        deployment_ref: str,
    ) -> ReservedContextClaims:
        """Verify a token and its deployment, audience, and lifetime bounds."""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError("wrong segment count")
            header_part, payload_part, signature_part = parts
            header = _decode_json_object(header_part)
            payload = _decode_json_object(payload_part)
            signature = _decode_segment(signature_part)
            if not signature:
                raise ValueError("empty signature")

            if (
                set(header) != {"alg", "kid", "typ", "v"}
                or header["alg"] != self._signer.algorithm()
                or not isinstance(header["kid"], str)
                or not header["kid"]
                or header["typ"] != _CONTEXT_TYPE
                or type(header["v"]) is not int
                or header["v"] != _SCHEMA_VERSION
            ):
                raise ValueError("invalid header")

            signing_input = f"{header_part}.{payload_part}".encode("ascii")
            if not self._signer.verify(signing_input, signature, header["kid"]):
                raise ValueError("invalid signature")

            claims = ReservedContextClaims.model_validate(payload)
            now = self._clock()
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
        except Exception as exc:
            raise GatewayContextError(_INVALID_CONTEXT, "reserved context is invalid") from exc
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


def _decode_segment(value: str) -> bytes:
    if not value or not _BASE64URL.fullmatch(value) or len(value) % 4 == 1:
        raise ValueError("invalid compact segment")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid compact segment") from exc


def _decode_json_object(value: str) -> dict[str, object]:
    raw = _decode_segment(value)
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
