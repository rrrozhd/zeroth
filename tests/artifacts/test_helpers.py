"""Tests for ArtifactReference extraction helpers and TTL refresh utility."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from zeroth.core.artifacts.errors import ArtifactTTLError
from zeroth.core.artifacts.helpers import extract_artifact_refs, refresh_artifact_ttls
from zeroth.core.artifacts.models import ArtifactReference


# ---------------------------------------------------------------------------
# extract_artifact_refs tests
# ---------------------------------------------------------------------------


class TestExtractArtifactRefs:
    """Tests for duck-typed ArtifactReference extraction from nested data."""

    def test_extracts_from_flat_dict(self) -> None:
        """Extracts ArtifactReference from a flat dict with all required fields."""
        data = {
            "result": {
                "store": "filesystem",
                "key": "run1/node1/abc123",
                "content_type": "image/png",
                "size": 1024,
            }
        }
        refs = extract_artifact_refs(data)
        assert len(refs) == 1
        assert refs[0].store == "filesystem"
        assert refs[0].key == "run1/node1/abc123"
        assert refs[0].content_type == "image/png"
        assert refs[0].size == 1024

    def test_extracts_from_nested_dict(self) -> None:
        """Extracts ArtifactReference from a dict nested 2 levels deep."""
        data = {
            "level1": {
                "level2": {
                    "store": "redis",
                    "key": "run2/node2/def456",
                    "content_type": "application/pdf",
                    "size": 2048,
                }
            }
        }
        refs = extract_artifact_refs(data)
        assert len(refs) == 1
        assert refs[0].store == "redis"
        assert refs[0].key == "run2/node2/def456"

    def test_extracts_multiple_from_list(self) -> None:
        """Extracts multiple ArtifactReferences from a list inside a dict."""
        data = {
            "artifacts": [
                {
                    "store": "filesystem",
                    "key": "run1/node1/aaa",
                    "content_type": "image/png",
                    "size": 100,
                },
                {
                    "store": "redis",
                    "key": "run1/node1/bbb",
                    "content_type": "text/plain",
                    "size": 200,
                },
            ]
        }
        refs = extract_artifact_refs(data)
        assert len(refs) == 2
        keys = {r.key for r in refs}
        assert keys == {"run1/node1/aaa", "run1/node1/bbb"}

    def test_ignores_partial_match(self) -> None:
        """Skips dicts that have some but not all required fields."""
        data = {
            "incomplete": {
                "store": "filesystem",
                "key": "run1/node1/abc",
                # Missing content_type and size
            }
        }
        refs = extract_artifact_refs(data)
        assert len(refs) == 0

    def test_returns_empty_for_no_refs(self) -> None:
        """Returns empty list when dict contains no artifact references."""
        data = {"name": "test", "value": 42, "nested": {"flag": True}}
        refs = extract_artifact_refs(data)
        assert refs == []

    def test_handles_none_gracefully(self) -> None:
        """Handles None values inside the data without crashing."""
        data = {"result": None, "other": {"value": None}}
        refs = extract_artifact_refs(data)
        assert refs == []

    def test_detects_ref_with_extra_metadata(self) -> None:
        """ArtifactReference with extra metadata field still detected via duck-typing."""
        data = {
            "output": {
                "store": "filesystem",
                "key": "run1/node1/ccc",
                "content_type": "application/json",
                "size": 512,
                "metadata": {"source": "agent-1"},
                "ttl_seconds": 3600,
            }
        }
        refs = extract_artifact_refs(data)
        assert len(refs) == 1
        assert refs[0].metadata == {"source": "agent-1"}
        assert refs[0].ttl_seconds == 3600

    def test_skips_invalid_types_in_required_fields(self) -> None:
        """Skips dicts where required fields have wrong types (model_validate fails)."""
        data = {
            "bad": {
                "store": "filesystem",
                "key": "run1/node1/abc",
                "content_type": "image/png",
                "size": "not_an_int",  # Wrong type
            }
        }
        refs = extract_artifact_refs(data)
        assert len(refs) == 0


# ---------------------------------------------------------------------------
# refresh_artifact_ttls tests
# ---------------------------------------------------------------------------


class TestRefreshArtifactTtls:
    """Tests for TTL refresh orchestration helper."""

    @pytest.mark.anyio()
    async def test_refreshes_all_refs(self) -> None:
        """Calls refresh_ttl on store for each extracted reference with the provided TTL."""
        store = AsyncMock()
        store.refresh_ttl = AsyncMock(return_value=True)
        data = {
            "artifacts": [
                {
                    "store": "filesystem",
                    "key": "run1/node1/aaa",
                    "content_type": "image/png",
                    "size": 100,
                },
                {
                    "store": "filesystem",
                    "key": "run1/node1/bbb",
                    "content_type": "text/plain",
                    "size": 200,
                },
            ]
        }
        count = await refresh_artifact_ttls(store, data, ttl=7200)
        assert count == 2
        assert store.refresh_ttl.call_count == 2
        store.refresh_ttl.assert_any_call("run1/node1/aaa", 7200)
        store.refresh_ttl.assert_any_call("run1/node1/bbb", 7200)

    @pytest.mark.anyio()
    async def test_returns_success_count(self) -> None:
        """Returns count of successfully refreshed artifacts."""
        store = AsyncMock()
        store.refresh_ttl = AsyncMock(return_value=True)
        data = {
            "ref": {
                "store": "filesystem",
                "key": "run1/node1/abc",
                "content_type": "image/png",
                "size": 100,
            }
        }
        count = await refresh_artifact_ttls(store, data, ttl=3600)
        assert count == 1

    @pytest.mark.anyio()
    async def test_handles_ttl_error_gracefully(self) -> None:
        """Silently handles ArtifactTTLError (artifact expired between extraction and refresh)."""
        store = AsyncMock()
        store.refresh_ttl = AsyncMock(
            side_effect=[True, ArtifactTTLError("expired"), True]
        )
        data = {
            "items": [
                {
                    "store": "fs",
                    "key": "run1/node1/a",
                    "content_type": "x",
                    "size": 1,
                },
                {
                    "store": "fs",
                    "key": "run1/node1/b",
                    "content_type": "x",
                    "size": 2,
                },
                {
                    "store": "fs",
                    "key": "run1/node1/c",
                    "content_type": "x",
                    "size": 3,
                },
            ]
        }
        count = await refresh_artifact_ttls(store, data, ttl=3600)
        # 2 succeeded, 1 got ArtifactTTLError
        assert count == 2

    @pytest.mark.anyio()
    async def test_noop_when_store_is_none(self) -> None:
        """Returns 0 when artifact_store is None."""
        count = await refresh_artifact_ttls(None, {"key": "value"}, ttl=3600)
        assert count == 0

    @pytest.mark.anyio()
    async def test_noop_when_no_refs_found(self) -> None:
        """Returns 0 when no artifact references found in data."""
        store = AsyncMock()
        count = await refresh_artifact_ttls(store, {"name": "test"}, ttl=3600)
        assert count == 0
        store.refresh_ttl.assert_not_called()
