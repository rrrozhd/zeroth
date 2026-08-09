"""Opaque tenant/workspace namespace adapter for shared artifact backends."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from zeroth.platform.artifacts.errors import (
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactTTLError,
)
from zeroth.platform.artifacts.models import ArtifactReference
from zeroth.platform.artifacts.store import ArtifactStore

_SCOPE_DOMAIN = b"zeroth-artifact-scope-v1\0"
_FRAMED_KEY_MARKER = "zeroth-run-v1"


def _without_nul(value: str, *, name: str) -> str:
    if not isinstance(value, str):
        raise ArtifactStorageError(f"{name} must be a string")
    if "\x00" in value:
        raise ArtifactStorageError(f"{name} must not contain NUL")
    return value


def _encode_segment(segment: str) -> str:
    raw = _without_nul(segment, name="artifact identifier segment").encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{len(raw)}-{encoded}"


def _validate_encoded_segment(value: str) -> str:
    """Validate and return one canonical length-prefixed base64url segment."""
    length_text, separator, encoded = value.partition("-")
    if not separator or not length_text.isascii() or not length_text.isdecimal():
        raise ArtifactStorageError("Malformed framed artifact key")
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        decoded = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ArtifactStorageError("Malformed framed artifact key") from None
    if len(raw) != int(length_text) or _encode_segment(decoded) != value:
        raise ArtifactStorageError("Malformed framed artifact key")
    return value


def frame_artifact_key(run_id: str, remainder: str) -> str:
    """Frame a slash-bearing run ID without making its key boundary ambiguous."""
    run_id = _without_nul(run_id, name="run_id")
    remainder = _without_nul(remainder, name="artifact key remainder")
    if not run_id or not remainder:
        raise ArtifactStorageError("Framed artifact key parts must not be empty")
    return f"{_FRAMED_KEY_MARKER}/{_encode_segment(run_id)}/{remainder}"


class TenantScopedArtifactStore:
    """Map logical artifact identifiers into one opaque physical scope."""

    def __init__(
        self,
        backend: ArtifactStore | Any,
        *,
        tenant_id: str,
        workspace_id: str | None = None,
    ) -> None:
        tenant_id = _without_nul(tenant_id, name="tenant_id")
        if workspace_id is not None:
            workspace_id = _without_nul(workspace_id, name="workspace_id")
        canonical = json.dumps(
            {"tenant_id": tenant_id, "workspace_id": workspace_id},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        self._backend = backend
        self._scope_digest = hashlib.sha256(_SCOPE_DOMAIN + canonical).hexdigest()
        self._object_root = f"scopes/v1/{self._scope_digest}/objects/v1"

    @property
    def scope_digest(self) -> str:
        """Return the non-secret stable digest used for this physical scope."""
        return self._scope_digest

    def _object_key(self, logical_key: str) -> str:
        logical_key = _without_nul(logical_key, name="artifact key")
        segments = logical_key.split("/")
        if segments[0] == _FRAMED_KEY_MARKER:
            if len(segments) < 3:
                raise ArtifactStorageError("Malformed framed artifact key")
            owner = _validate_encoded_segment(segments[1])
            framing = _encode_segment("framed")
            remainder = segments[2:]
        else:
            owner = _encode_segment(segments[0])
            framing = _encode_segment("legacy")
            remainder = segments[1:]
        encoded_remainder = "/".join(_encode_segment(segment) for segment in remainder)
        suffix = f"/{encoded_remainder}" if encoded_remainder else ""
        return f"{self._object_root}/{owner}/{framing}{suffix}"

    def _run_prefix(self, run_id: str) -> str:
        run_id = _without_nul(run_id, name="run_id")
        return f"{self._object_root}/{_encode_segment(run_id)}"

    def _receipt_id(self, logical_id: str) -> str:
        logical_id = _without_nul(logical_id, name="idempotency_key")
        digest = hashlib.sha256(logical_id.encode("utf-8")).hexdigest()
        return f"scope-v1-{self._scope_digest}-receipt-{digest}"

    async def store(
        self,
        key: str,
        data: bytes,
        content_type: str,
        ttl: int | None = None,
    ) -> ArtifactReference:
        reference = await self._backend.store(self._object_key(key), data, content_type, ttl=ttl)
        return reference.model_copy(update={"key": key})

    async def retrieve(self, key: str) -> bytes:
        try:
            return await self._backend.retrieve(self._object_key(key))
        except ArtifactNotFoundError:
            raise ArtifactNotFoundError(f"Artifact not found: {key}") from None

    async def delete(self, key: str, *, idempotency_key: str) -> bool:
        return await self._backend.delete(
            self._object_key(key),
            idempotency_key=self._receipt_id(idempotency_key),
        )

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        try:
            return await self._backend.refresh_ttl(self._object_key(key), ttl)
        except ArtifactTTLError:
            raise ArtifactTTLError(f"Cannot refresh TTL for missing artifact: {key}") from None

    async def exists(self, key: str) -> bool:
        return await self._backend.exists(self._object_key(key))

    async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
        return await self._backend.cleanup_run(
            self._run_prefix(run_id),
            idempotency_key=self._receipt_id(idempotency_key),
        )


__all__ = ["TenantScopedArtifactStore", "frame_artifact_key"]
