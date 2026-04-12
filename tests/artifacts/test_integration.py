"""Integration tests for artifact store wiring across platform subsystems.

Tests the full lifecycle: store artifact -> receive reference -> embed in node
output -> checkpoint triggers TTL refresh -> evidence export resolves payload ->
contract validates reference structure.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.core.artifacts.errors import ArtifactTTLError
from zeroth.core.artifacts.helpers import extract_artifact_refs, refresh_artifact_ttls
from zeroth.core.artifacts.models import ArtifactReference, ArtifactStoreSettings
from zeroth.core.artifacts.store import FilesystemArtifactStore
from zeroth.core.audit.evidence import (
    build_summary,
    resolve_artifact_references,
)
from zeroth.core.audit.models import NodeAuditRecord
from zeroth.core.contracts.registry import validate_artifact_reference
from zeroth.core.orchestrator.runtime import RuntimeOrchestrator


# ---------------------------------------------------------------------------
# Orchestrator integration tests
# ---------------------------------------------------------------------------


class TestOrchestratorArtifactIntegration:
    """Tests that RuntimeOrchestrator correctly wires artifact store."""

    def test_accepts_artifact_store_field(self) -> None:
        """RuntimeOrchestrator accepts optional artifact_store field."""
        run_repo = MagicMock()
        runners = {}
        eu_runner = MagicMock()
        store = MagicMock()

        orch = RuntimeOrchestrator(
            run_repository=run_repo,
            agent_runners=runners,
            executable_unit_runner=eu_runner,
            artifact_store=store,
        )
        assert orch.artifact_store is store

    def test_artifact_store_defaults_to_none(self) -> None:
        """When artifact_store is not provided, it defaults to None."""
        run_repo = MagicMock()
        runners = {}
        eu_runner = MagicMock()

        orch = RuntimeOrchestrator(
            run_repository=run_repo,
            agent_runners=runners,
            executable_unit_runner=eu_runner,
        )
        assert orch.artifact_store is None

    @pytest.mark.anyio()
    async def test_refresh_ttls_called_after_checkpoint(self) -> None:
        """After write_checkpoint, if artifact_store is set, refresh is called."""
        store = AsyncMock()
        store.refresh_ttl = AsyncMock(return_value=True)
        run_repo = MagicMock()
        runners = {}
        eu_runner = MagicMock()

        orch = RuntimeOrchestrator(
            run_repository=run_repo,
            agent_runners=runners,
            executable_unit_runner=eu_runner,
            artifact_store=store,
        )

        # Build a mock run with artifact references in execution history
        mock_run = MagicMock()
        mock_run.execution_history = [
            MagicMock(
                output_snapshot={
                    "result": {
                        "store": "filesystem",
                        "key": "run1/node1/abc",
                        "content_type": "image/png",
                        "size": 1024,
                    }
                }
            )
        ]
        mock_run.final_output = None

        await orch._refresh_artifact_ttls(mock_run)
        store.refresh_ttl.assert_called()

    @pytest.mark.anyio()
    async def test_no_refresh_when_store_none(self) -> None:
        """When artifact_store is None, no refresh calls happen."""
        run_repo = MagicMock()
        runners = {}
        eu_runner = MagicMock()

        orch = RuntimeOrchestrator(
            run_repository=run_repo,
            agent_runners=runners,
            executable_unit_runner=eu_runner,
        )

        mock_run = MagicMock()
        mock_run.execution_history = [
            MagicMock(
                output_snapshot={
                    "result": {
                        "store": "filesystem",
                        "key": "run1/node1/abc",
                        "content_type": "image/png",
                        "size": 1024,
                    }
                }
            )
        ]
        mock_run.final_output = None

        # Should not raise; it's a no-op
        await orch._refresh_artifact_ttls(mock_run)


# ---------------------------------------------------------------------------
# Audit evidence export tests
# ---------------------------------------------------------------------------


class TestAuditEvidenceArtifactIntegration:
    """Tests for artifact resolution in audit evidence export."""

    def test_build_summary_accepts_resolve_params(self) -> None:
        """build_summary accepts optional resolve_artifacts and artifact_store params."""
        result = build_summary([], [], resolve_artifacts=False, artifact_store=None)
        assert isinstance(result, dict)

    def test_build_summary_with_resolve_true(self) -> None:
        """When resolve_artifacts=True and artifact_store provided, summary includes flag."""
        store = MagicMock()
        result = build_summary([], [], resolve_artifacts=True, artifact_store=store)
        assert result["artifacts_resolved"] is True

    def test_build_summary_default_no_resolve(self) -> None:
        """When resolve_artifacts=False (default), no artifacts_resolved key."""
        result = build_summary([], [])
        assert "artifacts_resolved" not in result

    @pytest.mark.anyio()
    async def test_resolve_artifact_references(self) -> None:
        """resolve_artifact_references resolves ArtifactReferences to full payloads."""
        store = AsyncMock()
        payload = b"hello world"
        store.retrieve = AsyncMock(return_value=payload)

        audit = NodeAuditRecord(
            audit_id="test-1",
            run_id="run-1",
            thread_id="thread-1",
            node_id="node-1",
            node_version=1,
            graph_version_ref="g:v1",
            deployment_ref="dep-1",
            attempt=1,
            status="completed",
            output_snapshot={
                "file": {
                    "store": "filesystem",
                    "key": "run1/node1/abc",
                    "content_type": "image/png",
                    "size": 11,
                }
            },
        )

        resolved = await resolve_artifact_references([audit], store)
        assert len(resolved) == 1
        # Original should not be mutated
        assert "file" in audit.output_snapshot
        assert audit.output_snapshot["file"].get("_resolved_artifact") is None
        # Resolved copy should have the payload
        resolved_snapshot = resolved[0].output_snapshot
        file_data = resolved_snapshot["file"]
        assert file_data["_resolved_artifact"] == base64.b64encode(payload).decode()
        assert file_data["content_type"] == "image/png"
        assert file_data["size"] == 11

    @pytest.mark.anyio()
    async def test_resolve_leaves_non_refs_untouched(self) -> None:
        """resolve_artifact_references leaves non-artifact data untouched."""
        store = AsyncMock()

        audit = NodeAuditRecord(
            audit_id="test-1",
            run_id="run-1",
            thread_id="thread-1",
            node_id="node-1",
            node_version=1,
            graph_version_ref="g:v1",
            deployment_ref="dep-1",
            attempt=1,
            status="completed",
            output_snapshot={"result": "some text"},
        )

        resolved = await resolve_artifact_references([audit], store)
        assert resolved[0].output_snapshot == {"result": "some text"}
        store.retrieve.assert_not_called()


# ---------------------------------------------------------------------------
# Contract validation tests
# ---------------------------------------------------------------------------


class TestContractArtifactValidation:
    """Tests for ArtifactReference structural validation in contracts."""

    def test_valid_reference(self) -> None:
        """validate_artifact_reference accepts a valid ArtifactReference-shaped dict."""
        data = {
            "store": "filesystem",
            "key": "run1/node1/abc",
            "content_type": "image/png",
            "size": 1024,
        }
        assert validate_artifact_reference(data) is True

    def test_missing_required_fields(self) -> None:
        """validate_artifact_reference rejects a dict missing required fields."""
        data = {
            "store": "filesystem",
            "key": "run1/node1/abc",
            # Missing content_type and size
        }
        assert validate_artifact_reference(data) is False

    def test_wrong_field_types(self) -> None:
        """validate_artifact_reference rejects a dict with wrong field types."""
        data = {
            "store": "filesystem",
            "key": "run1/node1/abc",
            "content_type": "image/png",
            "size": "not_a_number",
        }
        assert validate_artifact_reference(data) is False


# ---------------------------------------------------------------------------
# Bootstrap wiring tests
# ---------------------------------------------------------------------------


class TestBootstrapArtifactWiring:
    """Tests for artifact store wiring in ServiceBootstrap."""

    def test_service_bootstrap_has_artifact_store_field(self) -> None:
        """ServiceBootstrap has artifact_store field defaulting to None."""
        from zeroth.core.service.bootstrap import ServiceBootstrap

        # Build with minimal required fields
        sb = ServiceBootstrap(
            deployment_service=MagicMock(),
            deployment=MagicMock(),
            graph=MagicMock(),
            run_repository=MagicMock(),
            thread_repository=MagicMock(),
            approval_service=MagicMock(),
            audit_repository=MagicMock(),
            contract_registry=MagicMock(),
            orchestrator=MagicMock(),
            auth_config=MagicMock(),
            authenticator=MagicMock(),
        )
        assert sb.artifact_store is None


# ---------------------------------------------------------------------------
# Full lifecycle integration test (filesystem-based, no Redis)
# ---------------------------------------------------------------------------


class TestFullLifecycleIntegration:
    """End-to-end lifecycle: store -> reference -> checkpoint -> refresh -> resolve."""

    @pytest.mark.anyio()
    async def test_store_checkpoint_refresh_resolve_lifecycle(self, tmp_path) -> None:
        """Full lifecycle: store artifact, get reference, refresh TTL, resolve in audit."""
        store = FilesystemArtifactStore(
            base_dir=str(tmp_path / "artifacts"),
            default_ttl=3600,
            max_size=10 * 1024 * 1024,
        )

        # 1. Store artifact
        key = "run-123/node-a/artifact-001"
        payload = b"PNG image data here..."
        ref = await store.store(key, payload, content_type="image/png", ttl=3600)
        assert isinstance(ref, ArtifactReference)
        assert ref.store == "filesystem"
        assert ref.key == key

        # 2. Embed reference in node output snapshot (as would appear in run state)
        output_snapshot = {
            "generated_image": ref.model_dump(mode="json"),
            "description": "A generated test image",
        }

        # 3. Extract refs (simulating orchestrator checkpoint scan)
        refs = extract_artifact_refs(output_snapshot)
        assert len(refs) == 1
        assert refs[0].key == key

        # 4. Refresh TTL (simulating checkpoint trigger)
        count = await refresh_artifact_ttls(store, output_snapshot, ttl=7200)
        assert count == 1

        # Verify artifact still exists after refresh
        assert await store.exists(key)

        # 5. Resolve in audit evidence
        audit = NodeAuditRecord(
            audit_id="audit-1",
            run_id="run-123",
            thread_id="thread-1",
            node_id="node-a",
            node_version=1,
            graph_version_ref="graph:v1",
            deployment_ref="dep-1",
            attempt=1,
            status="completed",
            output_snapshot=output_snapshot,
        )

        resolved = await resolve_artifact_references([audit], store)
        resolved_snap = resolved[0].output_snapshot
        assert resolved_snap["generated_image"]["_resolved_artifact"] is not None
        decoded = base64.b64decode(resolved_snap["generated_image"]["_resolved_artifact"])
        assert decoded == payload

        # 6. Contract validation on the reference
        assert validate_artifact_reference(ref.model_dump(mode="json")) is True

    @pytest.mark.anyio()
    async def test_bulk_cleanup(self, tmp_path) -> None:
        """cleanup_run deletes all artifacts for a run_id."""
        store = FilesystemArtifactStore(
            base_dir=str(tmp_path / "artifacts"),
            default_ttl=3600,
        )

        # Store multiple artifacts under the same run
        await store.store("run-99/node-a/001", b"data1", "text/plain", ttl=3600)
        await store.store("run-99/node-a/002", b"data2", "text/plain", ttl=3600)
        await store.store("run-99/node-b/003", b"data3", "text/plain", ttl=3600)

        assert await store.exists("run-99/node-a/001")

        count = await store.cleanup_run("run-99")
        assert count == 3

        assert not await store.exists("run-99/node-a/001")
        assert not await store.exists("run-99/node-a/002")
        assert not await store.exists("run-99/node-b/003")

    @pytest.mark.anyio()
    async def test_ttl_refresh_extends_lifetime(self, tmp_path) -> None:
        """TTL refresh extends artifact lifetime across multiple checkpoints."""
        store = FilesystemArtifactStore(
            base_dir=str(tmp_path / "artifacts"),
            default_ttl=60,
        )

        key = "run-50/node-x/art-001"
        await store.store(key, b"persistent data", "application/octet-stream", ttl=60)

        # Simulate multiple checkpoint refreshes
        for new_ttl in [120, 180, 240]:
            result = await store.refresh_ttl(key, new_ttl)
            assert result is True
            assert await store.exists(key)
