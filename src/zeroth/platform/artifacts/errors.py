"""Error types for the artifact store.

These exceptions are raised when something goes wrong while storing,
retrieving, or managing artifacts. They all inherit from ArtifactStoreError,
so you can catch that one base class to handle any artifact-related error.
"""

from __future__ import annotations


class ArtifactStoreError(Exception):
    """Base error for all artifact store operations.

    Catch this if you want to handle all artifact-related errors in one place.
    More specific errors below inherit from this one.
    """


class ArtifactNotFoundError(ArtifactStoreError):
    """Raised when a requested artifact key is missing from the store.

    This is raised when a retrieve or delete operation cannot find the
    artifact with the given key.
    """


class ArtifactStorageError(ArtifactStoreError):
    """Raised when a backend write or storage operation fails.

    This covers I/O errors, connection failures, and payload rejections
    such as exceeding the maximum artifact size.
    """


class ArtifactTTLError(ArtifactStoreError):
    """Raised when a TTL refresh targets an expired or missing artifact.

    This is raised when attempting to refresh the TTL of an artifact that
    no longer exists in the store.
    """
