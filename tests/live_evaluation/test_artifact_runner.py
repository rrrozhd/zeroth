"""Evaluation workflow artifacts use the real tenant-ready store contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from release.live_evaluation.action_runner import (
    EVALUATION_ARTIFACT_MANIFEST_REF,
    EvaluationActionRunner,
    EvaluationArtifactPayload,
)
from zeroth.contracts.graph import operation_identity
from zeroth.platform.artifacts.models import artifact_key_owner
from zeroth.platform.artifacts.store import FilesystemArtifactStore


def _identity(run_id: str = "artifact-run"):
    return operation_identity(
        run_id=run_id,
        dispatch_id=run_id,
        idempotency_key=run_id,
        attempt=0,
        target_ref=EVALUATION_ARTIFACT_MANIFEST_REF,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "media_type", "expected_prefix"),
    [
        ("json", "application/json", b'{"label":'),
        ("text", "text/plain", b"Zeroth artifact fixture:"),
        ("image", "image/png", b"\x89PNG\r\n\x1a\n"),
        ("binary", "application/octet-stream", b"ZEROTH-EVALUATION-BINARY\0"),
    ],
)
async def test_artifact_emitter_stores_run_owned_bytes(
    tmp_path, kind: str, media_type: str, expected_prefix: bytes
) -> None:
    store = FilesystemArtifactStore(tmp_path, default_ttl=3600, max_size=2_000_000)
    runner = EvaluationActionRunner(delegate=object(), sink=object(), artifact_store=store)

    result = await runner.run(
        EVALUATION_ARTIFACT_MANIFEST_REF,
        {"kind": kind, "label": f"{kind}-fixture", "size_bytes": 1_200_000},
        operation_identity=_identity(),
    )

    reference = result.output_data["artifact"]
    assert artifact_key_owner(reference["key"]) == "artifact-run"
    assert reference["content_type"] == media_type
    assert (await store.retrieve(reference["key"])).startswith(expected_prefix)
    assert result.audit_record["artifact_size"] == reference["size"]
    assert result.audit_record["cost_measurement"] == "measured"


@pytest.mark.asyncio
async def test_artifact_emitter_fails_closed_without_store() -> None:
    runner = EvaluationActionRunner(delegate=object(), sink=object())
    with pytest.raises(RuntimeError, match="artifact store is not configured"):
        await runner.run(
            EVALUATION_ARTIFACT_MANIFEST_REF,
            {"kind": "json", "label": "missing-store"},
            operation_identity=_identity(),
        )


def test_artifact_payload_is_bounded() -> None:
    with pytest.raises(ValidationError):
        EvaluationArtifactPayload(kind="binary", label="too-large", size_bytes=2_000_001)
