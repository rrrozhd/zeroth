"""Artifact storage for large payload externalization.

This package provides a pluggable artifact store that lets nodes externalize
large binary payloads (files, images, serialized data) to a backend store
and receive lightweight ArtifactReference pointers instead. Supports Redis
and filesystem backends with TTL management and prefix-based bulk cleanup.
"""

from zeroth.core.artifacts.errors import (
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactStoreError,
    ArtifactTTLError,
)
from zeroth.core.artifacts.models import (
    ArtifactReference,
    ArtifactStoreSettings,
    generate_artifact_key,
)
from zeroth.core.artifacts.store import (
    ArtifactStore,
    FilesystemArtifactStore,
    RedisArtifactStore,
)

__all__ = [
    "ArtifactNotFoundError",
    "ArtifactReference",
    "ArtifactStorageError",
    "ArtifactStore",
    "ArtifactStoreError",
    "ArtifactStoreSettings",
    "ArtifactTTLError",
    "FilesystemArtifactStore",
    "RedisArtifactStore",
    "generate_artifact_key",
]
