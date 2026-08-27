from __future__ import annotations

import json
from pathlib import Path

import pytest

from release.live_evaluation.campaign_execution import (
    CampaignExecutionSettings,
    build_campaign_execution,
)
from release.live_evaluation.campaign_http import ActionRecorder
from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.workflow1_scenarios import (
    ChromaCollectionFixtureBackend,
    LocalWorkflow1ScenarioController,
)


class _Collection:
    def __init__(self, name: str, rows: list[dict[str, object]]) -> None:
        self.name = name
        self.rows = {str(row["id"]): dict(row) for row in rows}

    def get(self, *, include):
        ordered = [self.rows[key] for key in sorted(self.rows)]
        return {
            "ids": [row["id"] for row in ordered],
            "documents": [row["document"] for row in ordered],
            "metadatas": [row["metadata"] for row in ordered],
            "embeddings": [row["embedding"] for row in ordered],
        }

    def delete(self, *, ids):
        for item_id in ids:
            self.rows.pop(item_id, None)

    def upsert(self, *, ids, documents, metadatas, embeddings):
        for item_id, document, metadata, embedding in zip(
            ids, documents, metadatas, embeddings, strict=True
        ):
            self.rows[item_id] = {
                "id": item_id,
                "document": document,
                "metadata": metadata,
                "embedding": embedding,
            }


def _execution():
    return build_campaign_execution(
        CampaignExecutionSettings(
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-studio-tenant",
            workspace_id=None,
            model="openai/gpt-4o-mini",
            embedding_model="openai/text-embedding-3-small",
            chroma_connector_ref="eval_chroma_v1",
        )
    )


def _action(scenario: str):
    return next(action for action in _execution().actions if action.scenario == scenario)


def _rows() -> list[dict[str, object]]:
    return [
        {
            "id": "known-a",
            "document": json.dumps({"text": "The synthetic status is stable."}),
            "metadata": {"key": "known-a", "scope": "shared"},
            "embedding": [0.1, 0.2, 0.3],
        },
        {
            "id": "known-b",
            "document": json.dumps({"text": "The synthetic owner is team one."}),
            "metadata": {"key": "known-b", "scope": "shared"},
            "embedding": [0.3, 0.2, 0.1],
        },
        {
            "id": "known-c",
            "document": json.dumps({"text": "The synthetic deadline is Friday."}),
            "metadata": {"key": "known-c", "scope": "shared"},
            "embedding": [0.2, 0.3, 0.1],
        },
    ]


def _controller(tmp_path: Path, collection: _Collection):
    store = EvidenceStore(tmp_path / "evidence")
    recorder = ActionRecorder(store, step_id="w1-conflict", command_sequence=1)
    backend = ChromaCollectionFixtureBackend(
        collection,
        tenant_id="evaluation-studio-tenant",
    )
    return (
        LocalWorkflow1ScenarioController(
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-studio-tenant",
            state_root=tmp_path / "external-state",
            corpus=backend,
        ),
        recorder,
    )


def test_conflict_fixture_is_campaign_scoped_and_restores_exactly_after_restart(
    tmp_path: Path,
) -> None:
    collection = _Collection(
        "eval_shared_evaluation_studio_tenant_b4f207fd_shared",
        _rows(),
    )
    original = collection.get(include=[])
    controller, recorder = _controller(tmp_path, collection)

    prepared = controller.prepare(_action("negative-conflicting-document"), recorder)

    assert set(collection.rows) == {"conflict-a", "conflict-b"}
    assert "opposing" in str(collection.rows["conflict-a"]["document"]).lower()
    assert "opposing" in str(collection.rows["conflict-b"]["document"]).lower()
    assert prepared.checkpoint_id.startswith("evaluation-studio-v1:")
    assert all(reference.startswith("events.ndjson#") for reference in prepared.evidence)

    # A newly constructed controller can recover the durable checkpoint.
    restarted, restarted_recorder = _controller(tmp_path, collection)
    restored = restarted.restore(
        _action("negative-conflicting-document"), prepared, restarted_recorder
    )

    assert collection.get(include=[]) == original
    assert all(reference.startswith("events.ndjson#") for reference in restored)


def test_restore_fails_closed_when_corpus_changed_after_fixture_install(tmp_path: Path) -> None:
    collection = _Collection(
        "eval_shared_evaluation_studio_tenant_b4f207fd_shared",
        _rows(),
    )
    controller, recorder = _controller(tmp_path, collection)
    action = _action("negative-conflicting-document")
    prepared = controller.prepare(action, recorder)
    collection.rows["unexpected"] = dict(_rows()[0], id="unexpected")

    with pytest.raises(RuntimeError, match="fixture corpus drifted"):
        controller.restore(action, prepared, recorder)

    assert "unexpected" in collection.rows


def test_excessive_revision_is_not_a_corpus_fixture_scenario(
    tmp_path: Path,
) -> None:
    collection = _Collection(
        "eval_shared_evaluation_studio_tenant_b4f207fd_shared",
        _rows(),
    )
    original = collection.get(include=[])
    controller, recorder = _controller(tmp_path, collection)

    with pytest.raises(ValueError, match="unsupported stateful Workflow 1 scenario"):
        controller.prepare(_action("negative-excessive-revision"), recorder)

    assert collection.get(include=[]) == original


def test_collection_backend_rejects_wrong_tenant_namespace_before_mutation(
    tmp_path: Path,
) -> None:
    collection = _Collection("eval_shared_some_other_tenant_shared", _rows())
    original = collection.get(include=[])

    with pytest.raises(ValueError, match="tenant namespace"):
        _controller(tmp_path, collection)

    assert collection.get(include=[]) == original


def test_conflict_checkpoint_rejects_secret_shaped_corpus_before_mutation(tmp_path: Path) -> None:
    rows = _rows()
    rows[0]["document"] = json.dumps({"api_key": "not-allowed-in-fixture-state"})
    collection = _Collection(
        "eval_shared_evaluation_studio_tenant_b4f207fd_shared",
        rows,
    )
    original = collection.get(include=[])
    controller, recorder = _controller(tmp_path, collection)

    with pytest.raises(ValueError, match="secret-shaped"):
        controller.prepare(_action("negative-conflicting-document"), recorder)

    assert collection.get(include=[]) == original
