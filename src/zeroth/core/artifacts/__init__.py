"""Legacy import path for the platform artifacts package.

Artifact storage lives in :mod:`zeroth.platform.artifacts`; this package
republishes the same objects for compatibility. Import from the canonical
location instead (see docs/backend-import-migration.md).
"""

from zeroth.platform.artifacts import (
    ArtifactNotFoundError,
    ArtifactReference,
    ArtifactStorageError,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactStoreSettings,
    ArtifactTTLError,
    FilesystemArtifactStore,
    RedisArtifactStore,
    generate_artifact_key,
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
