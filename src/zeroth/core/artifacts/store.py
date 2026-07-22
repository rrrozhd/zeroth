"""Legacy import path for :mod:`zeroth.platform.artifacts.store`."""

from zeroth.platform.artifacts.store import (
    ArtifactStore,
    FilesystemArtifactStore,
    RedisArtifactStore,
)

__all__ = [
    "ArtifactStore",
    "FilesystemArtifactStore",
    "RedisArtifactStore",
]
