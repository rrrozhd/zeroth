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
from zeroth.platform.artifacts.models import (
    ArtifactReference,
    parse_framed_artifact_key,
    parse_generated_artifact_key,
)
from zeroth.platform.artifacts.models import (
    frame_artifact_key as _frame_artifact_key,
)
from zeroth.platform.artifacts.store import ArtifactStore

_SCOPE_DOMAIN = b"zeroth-artifact-scope-v1\0"


def _without_nul(value: str, *, name: str) -> str:
    """Validate a namespace value without echoing unsafe input."""
    if not isinstance(value, str):
        raise ArtifactStorageError(f"{name} must be a string")
    if "\x00" in value:
        raise ArtifactStorageError(f"{name} must not contain NUL")
    return value


def _encode_segment(segment: str) -> str:
    """Encode one logical key segment with an unambiguous byte length."""
    raw = _without_nul(segment, name="artifact identifier segment").encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{len(raw)}-{encoded}"


def frame_artifact_key(run_id: str, remainder: str) -> str:
    """Frame a slash-bearing run ID without making its key boundary ambiguous."""
    try:
        return _frame_artifact_key(run_id, remainder)
    except ValueError as exc:
        raise ArtifactStorageError(str(exc)) from None


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
        """Map a logical artifact key into this store's opaque namespace."""
        logical_key = _without_nul(logical_key, name="artifact key")
        segments = logical_key.split("/")
        try:
            framed = parse_framed_artifact_key(logical_key)
        except ValueError:
            if parse_generated_artifact_key(logical_key) is None:
                raise ArtifactStorageError("Malformed framed artifact key") from None
            framed = None
        if framed is not None:
            run_id, remainder_text = framed
            owner = _encode_segment(run_id)
            framing = _encode_segment("framed")
            remainder = remainder_text.split("/")
        else:
            owner = _encode_segment(segments[0])
            framing = _encode_segment("legacy")
            remainder = segments[1:]
        encoded_remainder = "/".join(_encode_segment(segment) for segment in remainder)
        suffix = f"/{encoded_remainder}" if encoded_remainder else ""
        return f"{self._object_root}/{owner}/{framing}{suffix}"

    def _run_prefix(self, run_id: str) -> str:
        """Return the exact physical owner prefix for one logical run."""
        run_id = _without_nul(run_id, name="run_id")
        return f"{self._object_root}/{_encode_segment(run_id)}"

    def _receipt_id(self, logical_id: str) -> str:
        """Bind an idempotency identifier to this tenant scope."""
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
        """Store an artifact while preserving its logical reference."""
        reference = await self._backend.store(self._object_key(key), data, content_type, ttl=ttl)
        return reference.model_copy(update={"key": key})

    async def retrieve(self, key: str) -> bytes:
        """Retrieve an artifact only from this tenant scope."""
        try:
            return await self._backend.retrieve(self._object_key(key))
        except ArtifactNotFoundError:
            raise ArtifactNotFoundError(f"Artifact not found: {key}") from None

    async def delete(self, key: str, *, idempotency_key: str) -> bool:
        """Delete a scoped artifact with a scope-bound receipt."""
        return await self._backend.delete(
            self._object_key(key),
            idempotency_key=self._receipt_id(idempotency_key),
        )

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        """Refresh a scoped artifact's time-to-live."""
        try:
            return await self._backend.refresh_ttl(self._object_key(key), ttl)
        except ArtifactTTLError:
            raise ArtifactTTLError(f"Cannot refresh TTL for missing artifact: {key}") from None

    async def exists(self, key: str) -> bool:
        """Return whether a logical artifact exists in this scope."""
        return await self._backend.exists(self._object_key(key))

    async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
        """Remove artifacts owned by exactly one scoped logical run."""
        return await self._backend.cleanup_run(
            self._run_prefix(run_id),
            idempotency_key=self._receipt_id(idempotency_key),
        )


__all__ = ["TenantScopedArtifactStore", "frame_artifact_key"]
