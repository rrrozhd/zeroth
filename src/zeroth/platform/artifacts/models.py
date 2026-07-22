"""Artifact reference model, settings, and key generation.

Defines the ArtifactReference Pydantic model that serves as a lightweight
pointer to externalized large payloads. Also includes ArtifactStoreSettings
for configuration and a key generation function that produces hierarchical
keys suitable for prefix-based bulk cleanup.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


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
    """Hierarchical key in {run_id}/{node_id}/{uuid} format."""

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

    Keys follow the ``{run_id}/{node_id}/{uuid4_hex}`` pattern.
    The run_id prefix enables efficient bulk cleanup of all artifacts
    belonging to a specific run via prefix scan or directory deletion.

    Args:
        run_id: The run identifier for namespacing.
        node_id: The node identifier within the run.

    Returns:
        A unique key string in ``{run_id}/{node_id}/{uuid4_hex}`` format.
    """
    return f"{run_id}/{node_id}/{uuid4().hex}"
