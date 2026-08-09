"""Artifact reference model, settings, and key generation.

Defines the ArtifactReference Pydantic model that serves as a lightweight
pointer to externalized large payloads. Also includes ArtifactStoreSettings
for configuration and a key generation function that produces hierarchical
keys suitable for prefix-based bulk cleanup.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

_FRAMED_KEY_MARKER = "zeroth-run-v1"


def _encode_key_segment(value: str) -> str:
    raw = value.encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{len(raw)}-{encoded}"


def _decode_key_segment(value: str) -> str:
    length_text, separator, encoded = value.partition("-")
    if not separator or not length_text.isascii() or not length_text.isdecimal():
        raise ValueError("malformed framed artifact key")
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        decoded = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("malformed framed artifact key") from None
    if len(raw) != int(length_text) or _encode_key_segment(decoded) != value:
        raise ValueError("malformed framed artifact key")
    return decoded


def frame_artifact_key(run_id: str, remainder: str) -> str:
    """Frame a run ID so slash bytes cannot blur its logical key boundary."""
    if not run_id or not remainder or "\x00" in run_id or "\x00" in remainder:
        raise ValueError("framed artifact key parts must be non-empty and NUL-free")
    return f"{_FRAMED_KEY_MARKER}/{_encode_key_segment(run_id)}/{remainder}"


def parse_framed_artifact_key(key: str) -> tuple[str, str] | None:
    """Return ``(run_id, remainder)`` for a framed key, or None for legacy keys."""
    segments = key.split("/")
    if not segments or segments[0] != _FRAMED_KEY_MARKER:
        return None
    if len(segments) < 3:
        raise ValueError("malformed framed artifact key")
    run_id = _decode_key_segment(segments[1])
    remainder = "/".join(segments[2:])
    if not run_id or not remainder or "\x00" in remainder:
        raise ValueError("malformed framed artifact key")
    return run_id, remainder


def artifact_key_owner(key: str) -> str | None:
    """Return the unambiguous run owner of a framed or legacy logical key."""
    if not key or "\x00" in key:
        return None
    try:
        framed = parse_framed_artifact_key(key)
    except ValueError:
        return None
    return framed[0] if framed is not None else key.split("/", 1)[0]


def parse_generated_artifact_key(key: str) -> tuple[str, str, str] | None:
    """Parse the complete grammar minted by :func:`generate_artifact_key`."""
    owner = artifact_key_owner(key)
    if owner is None:
        return None
    try:
        framed = parse_framed_artifact_key(key)
    except ValueError:
        return None
    remainder = framed[1] if framed is not None else key.removeprefix(f"{owner}/")
    parts = remainder.split("/")
    if len(parts) != 2:
        return None
    return owner, parts[0], parts[1]


class ArtifactReference(BaseModel):
    """Lightweight pointer to an externalized artifact in a storage backend.

    Nodes produce these references instead of carrying large binary payloads
    inline. The reference contains enough metadata to retrieve, manage TTL,
    and audit the artifact.
    """

    model_config = ConfigDict(extra="forbid")

    store: str
    """Storage backend identifier, e.g. 'redis' or 'filesystem'."""

    key: str
    """Logical key in the generated legacy or framed-v1 grammar."""

    content_type: str
    """MIME type of the stored artifact."""

    size: int
    """Size of the artifact in bytes."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """UTC timestamp when the artifact was stored."""

    ttl_seconds: int | None = None
    """Time-to-live in seconds. None means no expiration."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Optional key-value metadata attached to the artifact."""


class ArtifactStoreSettings(BaseModel):
    """Configuration for the artifact storage subsystem.

    Wired into ZerothSettings as the ``artifact_store`` field.
    Controls which backend to use, TTL defaults, filesystem paths,
    Redis key prefixes, and size limits.
    """

    model_config = ConfigDict(extra="forbid")

    backend: str = "filesystem"
    """Storage backend: 'filesystem' or 'redis'."""

    default_ttl_seconds: int = 3600
    """Default TTL for artifacts in seconds (1 hour)."""

    filesystem_base_dir: str = ".zeroth/artifacts"
    """Base directory for filesystem backend storage."""

    redis_key_prefix: str = "zeroth:artifact"
    """Key prefix for Redis backend to avoid namespace collisions."""

    max_artifact_size_bytes: int = 104857600
    """Maximum artifact size in bytes (default 100 MB)."""


def generate_artifact_key(run_id: str, node_id: str) -> str:
    """Generate a hierarchical artifact key.

    Simple run IDs retain the legacy ``{run_id}/{node_id}/{uuid4_hex}``
    pattern. Slash-bearing or reserved run IDs use the explicit v1 frame so
    the run boundary remains unambiguous during bulk cleanup.

    Args:
        run_id: The run identifier for namespacing.
        node_id: The node identifier within the run.

    Returns:
        A unique legacy or explicitly framed logical artifact key.
    """
    remainder = f"{node_id}/{uuid4().hex}"
    needs_frame = "/" in run_id or run_id == _FRAMED_KEY_MARKER
    return frame_artifact_key(run_id, remainder) if needs_frame else f"{run_id}/{remainder}"
