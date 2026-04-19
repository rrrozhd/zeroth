"""Tests for artifact reference model, settings, errors, and key generation."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zeroth.core.artifacts import (
    ArtifactNotFoundError,
    ArtifactReference,
    ArtifactStore,
    ArtifactStorageError,
    ArtifactStoreError,
    ArtifactStoreSettings,
    ArtifactTTLError,
    FilesystemArtifactStore,
    RedisArtifactStore,
    generate_artifact_key,
)
from zeroth.core.artifacts.errors import (
    ArtifactNotFoundError as DirectNotFoundError,
    ArtifactStorageError as DirectStorageError,
    ArtifactStoreError as DirectStoreError,
    ArtifactTTLError as DirectTTLError,
)
from zeroth.core.artifacts.models import (
    ArtifactReference as DirectReference,
    ArtifactStoreSettings as DirectSettings,
    generate_artifact_key as direct_generate_key,
)


class TestArtifactReference:
    """Tests for the ArtifactReference Pydantic model."""

    def test_construction_with_required_fields(self) -> None:
        """ArtifactReference can be constructed with all required fields."""
        ref = ArtifactReference(
            store="redis",
            key="run1/node1/abc123",
            content_type="application/json",
            size=1024,
        )
        assert ref.store == "redis"
        assert ref.key == "run1/node1/abc123"
        assert ref.content_type == "application/json"
        assert ref.size == 1024

    def test_defaults_created_at_utc(self) -> None:
        """created_at auto-populates to UTC datetime."""
        before = datetime.now(UTC)
        ref = ArtifactReference(
            store="filesystem",
            key="run1/node1/abc123",
            content_type="text/plain",
            size=512,
        )
        after = datetime.now(UTC)
        assert ref.created_at >= before
        assert ref.created_at <= after
        assert ref.created_at.tzinfo is not None

    def test_defaults_ttl_seconds_none(self) -> None:
        """ttl_seconds defaults to None."""
        ref = ArtifactReference(
            store="redis",
            key="run1/node1/abc123",
            content_type="text/plain",
            size=100,
        )
        assert ref.ttl_seconds is None

    def test_defaults_metadata_empty_dict(self) -> None:
        """metadata defaults to empty dict."""
        ref = ArtifactReference(
            store="redis",
            key="run1/node1/abc123",
            content_type="text/plain",
            size=100,
        )
        assert ref.metadata == {}

    def test_extra_forbid_rejects_unknown_fields(self) -> None:
        """ArtifactReference rejects unknown fields due to extra='forbid'."""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ArtifactReference(
                store="redis",
                key="run1/node1/abc123",
                content_type="text/plain",
                size=100,
                unknown_field="bad",
            )

    def test_json_round_trip(self) -> None:
        """model_dump(mode='json') produces serializable dict that round-trips."""
        ref = ArtifactReference(
            store="redis",
            key="run1/node1/abc123",
            content_type="application/octet-stream",
            size=2048,
            ttl_seconds=600,
            metadata={"label": "screenshot"},
        )
        data = ref.model_dump(mode="json")
        assert isinstance(data, dict)
        # All values should be JSON-serializable types
        assert isinstance(data["created_at"], str)
        assert data["store"] == "redis"
        assert data["ttl_seconds"] == 600
        assert data["metadata"] == {"label": "screenshot"}

        # Round-trip: reconstruct from JSON dict
        recovered = ArtifactReference.model_validate(data)
        assert recovered.store == ref.store
        assert recovered.key == ref.key
        assert recovered.content_type == ref.content_type
        assert recovered.size == ref.size
        assert recovered.ttl_seconds == ref.ttl_seconds
        assert recovered.metadata == ref.metadata


class TestGenerateArtifactKey:
    """Tests for the generate_artifact_key function."""

    def test_key_format(self) -> None:
        """Key follows {run_id}/{node_id}/{uuid_hex} format."""
        key = generate_artifact_key("run-abc", "node-xyz")
        parts = key.split("/")
        assert len(parts) == 3
        assert parts[0] == "run-abc"
        assert parts[1] == "node-xyz"
        # UUID hex portion is 32 hex characters
        assert re.fullmatch(r"[0-9a-f]{32}", parts[2])

    def test_uniqueness(self) -> None:
        """Repeated calls with same run_id/node_id produce unique keys."""
        keys = {generate_artifact_key("run-1", "node-1") for _ in range(100)}
        assert len(keys) == 100


class TestArtifactStoreSettings:
    """Tests for ArtifactStoreSettings configuration model."""

    def test_defaults(self) -> None:
        """Default values are correct."""
        settings = ArtifactStoreSettings()
        assert settings.backend == "filesystem"
        assert settings.default_ttl_seconds == 3600
        assert settings.filesystem_base_dir == ".zeroth/artifacts"
        assert settings.redis_key_prefix == "zeroth:artifact"

    def test_extra_forbid(self) -> None:
        """Rejects unknown fields."""
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ArtifactStoreSettings(bad_field="oops")


class TestErrorHierarchy:
    """Tests for the artifact store error hierarchy."""

    def test_base_error_is_exception(self) -> None:
        """ArtifactStoreError inherits from Exception."""
        assert issubclass(ArtifactStoreError, Exception)

    def test_not_found_inherits_base(self) -> None:
        """ArtifactNotFoundError inherits from ArtifactStoreError."""
        assert issubclass(ArtifactNotFoundError, ArtifactStoreError)

    def test_storage_error_inherits_base(self) -> None:
        """ArtifactStorageError inherits from ArtifactStoreError."""
        assert issubclass(ArtifactStorageError, ArtifactStoreError)

    def test_ttl_error_inherits_base(self) -> None:
        """ArtifactTTLError inherits from ArtifactStoreError."""
        assert issubclass(ArtifactTTLError, ArtifactStoreError)

    def test_catch_base_catches_all(self) -> None:
        """Catching ArtifactStoreError catches all sub-errors."""
        for error_cls in (ArtifactNotFoundError, ArtifactStorageError, ArtifactTTLError):
            with pytest.raises(ArtifactStoreError):
                raise error_cls("test")


class TestPublicExports:
    """Tests that all public symbols are exported from __init__.py."""

    def test_all_symbols_exported(self) -> None:
        """All required public symbols are accessible from zeroth.core.artifacts."""
        import zeroth.core.artifacts as artifacts

        expected = [
            "ArtifactStore",
            "ArtifactReference",
            "ArtifactStoreSettings",
            "RedisArtifactStore",
            "FilesystemArtifactStore",
            "ArtifactStoreError",
            "ArtifactNotFoundError",
            "ArtifactStorageError",
            "ArtifactTTLError",
            "generate_artifact_key",
        ]
        for name in expected:
            assert hasattr(artifacts, name), f"Missing export: {name}"

    def test_all_list_matches(self) -> None:
        """__all__ contains all expected symbols."""
        import zeroth.core.artifacts as artifacts

        expected = {
            "ArtifactStore",
            "ArtifactReference",
            "ArtifactStoreSettings",
            "RedisArtifactStore",
            "FilesystemArtifactStore",
            "ArtifactStoreError",
            "ArtifactNotFoundError",
            "ArtifactStorageError",
            "ArtifactTTLError",
            "generate_artifact_key",
        }
        assert expected.issubset(set(artifacts.__all__))


class TestSettingsIntegration:
    """Tests that ArtifactStoreSettings is wired into ZerothSettings."""

    def test_zeroth_settings_has_artifact_store(self) -> None:
        """ZerothSettings includes artifact_store field."""
        from zeroth.core.config.settings import ZerothSettings

        fields = ZerothSettings.model_fields
        assert "artifact_store" in fields

    def test_default_artifact_store_settings(self) -> None:
        """ZerothSettings default artifact_store is an ArtifactStoreSettings instance."""
        from zeroth.core.config.settings import ZerothSettings

        settings = ZerothSettings()
        assert isinstance(settings.artifact_store, ArtifactStoreSettings)
        assert settings.artifact_store.backend == "filesystem"
