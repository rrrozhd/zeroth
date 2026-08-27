"""Campaign-local fixture control for stateful Workflow 1 negative cases.

The controller owns only external evaluation state.  It never embeds content or
calls a provider: conflict records reuse vectors already present in the seeded
tenant collection, and restoration is verified byte-for-byte through a canonical
snapshot digest.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from zeroth.integrations.memory.tenant_scoped import tenant_slug

from .campaign_execution import WorkflowAction
from .campaign_http import PreparedWorkflow1Scenario
from .coordinator import ActionRecorder
from .evidence import CorrelationIds

_SECRET_TEXT = re.compile(
    r"(?:\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}|\bAuthorization\s*:|"
    r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}|"
    r'"(?:api_?key|provider_?key|service_?key|secret|token|authorization)"\s*:)',
    re.IGNORECASE,
)
_SECRET_KEY = re.compile(
    r"^(?:api_?key|provider_?key|service_?key|secret|token|authorization)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CorpusSnapshot:
    rows: tuple[dict[str, object], ...]
    digest: str


class Workflow1CorpusFixtureBackend(Protocol):
    def snapshot(self) -> CorpusSnapshot: ...

    def install_conflict(self, source: CorpusSnapshot) -> CorpusSnapshot: ...

    def replace(self, snapshot: CorpusSnapshot) -> CorpusSnapshot: ...


def _plain(value: object) -> object:
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        value = tolist()
    if isinstance(value, dict):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"corpus snapshot contains unsupported {type(value).__name__}")


def _reject_secret_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_KEY.fullmatch(str(key)):
                raise ValueError("corpus fixture contains secret-shaped content")
            _reject_secret_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_fields(child)


def _snapshot(rows: list[dict[str, object]]) -> CorpusSnapshot:
    plain_rows = [_plain(row) for row in rows]
    if not all(isinstance(row, dict) for row in plain_rows):
        raise TypeError("corpus snapshot rows must be objects")
    normalized = tuple(sorted(plain_rows, key=lambda row: str(row["id"])))
    ids: list[str] = []
    for row in normalized:
        item_id = row.get("id")
        document = row.get("document")
        embedding = row.get("embedding")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("corpus snapshot contains an invalid document ID")
        if not isinstance(document, str):
            raise ValueError("corpus snapshot contains a non-text document")
        try:
            decoded_document = json.loads(document)
        except json.JSONDecodeError as exc:
            raise ValueError("corpus snapshot document is not JSON") from exc
        _reject_secret_fields(decoded_document)
        _reject_secret_fields(row.get("metadata"))
        if (
            not isinstance(embedding, list)
            or not embedding
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in embedding
            )
        ):
            raise ValueError("corpus snapshot contains an invalid embedding")
        if not isinstance(row.get("metadata"), dict):
            raise ValueError("corpus snapshot contains invalid metadata")
        ids.append(item_id)
    if len(ids) != len(set(ids)):
        raise ValueError("corpus snapshot contains duplicate document IDs")
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if _SECRET_TEXT.search(encoded):
        raise ValueError("corpus fixture contains secret-shaped content")
    return CorpusSnapshot(
        rows=normalized,  # type: ignore[arg-type]
        digest=hashlib.sha256(encoded.encode()).hexdigest(),
    )


class ChromaCollectionFixtureBackend:
    """Provider-free snapshot/replace adapter over one raw Chroma collection."""

    def __init__(self, collection: object, *, tenant_id: str) -> None:
        self.collection = collection
        name = getattr(collection, "name", None)
        tenant_hash = tenant_slug(tenant_id).rsplit("-", 1)[-1]
        if not isinstance(name, str) or tenant_hash not in name:
            raise ValueError("Chroma fixture collection does not match the tenant namespace")

    def snapshot(self) -> CorpusSnapshot:
        getter = getattr(self.collection, "get", None)
        if not callable(getter):
            raise TypeError("Chroma fixture collection must implement get")
        result = getter(include=["documents", "metadatas", "embeddings"])
        if not isinstance(result, dict):
            raise RuntimeError("Chroma fixture snapshot returned a malformed response")
        fields = [
            _plain(result.get(name)) for name in ("ids", "documents", "metadatas", "embeddings")
        ]
        if not all(isinstance(field, list) for field in fields):
            raise RuntimeError("Chroma fixture snapshot omitted a required field")
        ids, documents, metadatas, embeddings = fields
        if len({len(ids), len(documents), len(metadatas), len(embeddings)}) != 1:
            raise RuntimeError("Chroma fixture snapshot fields have different lengths")
        rows = [
            {
                "id": item_id,
                "document": document,
                "metadata": metadata or {},
                "embedding": embedding,
            }
            for item_id, document, metadata, embedding in zip(
                ids, documents, metadatas, embeddings, strict=True
            )
        ]
        return _snapshot(rows)

    def install_conflict(self, source: CorpusSnapshot) -> CorpusSnapshot:
        if len(source.rows) < 2:
            raise RuntimeError("conflict fixture requires two pre-seeded embedding vectors")
        fixtures = [
            {
                "id": "conflict-a",
                "document": json.dumps(
                    {
                        "text": (
                            "Source A reports one synthetic fact; "
                            "Source B reports the opposing value."
                        ),
                        "synthetic_fact": "alpha",
                    },
                    sort_keys=True,
                ),
                "metadata": {"fixture": "conflict", "source_id": "conflict-a"},
                "embedding": source.rows[0]["embedding"],
            },
            {
                "id": "conflict-b",
                "document": json.dumps(
                    {
                        "text": (
                            "Source B reports the opposing synthetic fact; Source A reports alpha."
                        ),
                        "synthetic_fact": "beta",
                    },
                    sort_keys=True,
                ),
                "metadata": {"fixture": "conflict", "source_id": "conflict-b"},
                "embedding": source.rows[1]["embedding"],
            },
        ]
        return self.replace(_snapshot(fixtures))

    def replace(self, snapshot: CorpusSnapshot) -> CorpusSnapshot:
        current = self.snapshot()
        delete = getattr(self.collection, "delete", None)
        upsert = getattr(self.collection, "upsert", None)
        if not callable(delete) or not callable(upsert):
            raise TypeError("Chroma fixture collection must implement delete and upsert")
        if current.rows:
            delete(ids=[str(row["id"]) for row in current.rows])
        if snapshot.rows:
            upsert(
                ids=[str(row["id"]) for row in snapshot.rows],
                documents=[str(row["document"]) for row in snapshot.rows],
                metadatas=[dict(row["metadata"]) for row in snapshot.rows],  # type: ignore[arg-type]
                embeddings=[list(row["embedding"]) for row in snapshot.rows],  # type: ignore[arg-type]
            )
        observed = self.snapshot()
        if observed.digest != snapshot.digest:
            raise RuntimeError("Chroma fixture replacement did not verify")
        return observed


class LocalWorkflow1ScenarioController:
    """Durable, campaign-scoped controller for stateful Workflow 1 fixtures."""

    def __init__(
        self,
        *,
        campaign_id: str,
        tenant_id: str,
        state_root: Path,
        corpus: Workflow1CorpusFixtureBackend,
    ) -> None:
        if not campaign_id or not tenant_id:
            raise ValueError("campaign and tenant identity are required")
        self.campaign_id = campaign_id
        self.tenant_id = tenant_id
        self.corpus = corpus
        root = state_root.expanduser().resolve(strict=False) / campaign_id
        root.mkdir(parents=True, exist_ok=True)
        self.database_path = root / "workflow1-scenarios.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow1_fixture_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    scenario TEXT NOT NULL,
                    original_json TEXT NOT NULL,
                    original_digest TEXT NOT NULL,
                    installed_digest TEXT,
                    state TEXT NOT NULL CHECK (state IN ('prepared', 'installed', 'restored')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    restored_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow1_active_fixture
                ON workflow1_fixture_checkpoints (campaign_id, tenant_id)
                WHERE state != 'restored'
                """
            )

    def _validate_action(self, action: WorkflowAction) -> None:
        if action.workflow != "workflow1" or action.action_type != "negative":
            raise ValueError("Workflow 1 scenario controller received an incompatible action")
        if action.request.identity.campaign_id != self.campaign_id:
            raise ValueError("workflow1 scenario campaign identity does not match")

    def prepare(
        self, action: WorkflowAction, recorder: ActionRecorder
    ) -> PreparedWorkflow1Scenario:
        self._validate_action(action)
        if action.scenario != "negative-conflicting-document":
            raise ValueError(f"unsupported stateful Workflow 1 scenario: {action.scenario}")

        original = self.corpus.snapshot()
        if len(original.rows) != 3:
            raise RuntimeError("conflict fixture requires the exact three-document seeded corpus")
        checkpoint_id = f"{self.campaign_id}:{uuid4().hex}"
        original_json = json.dumps(original.rows, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT checkpoint_id FROM workflow1_fixture_checkpoints
                WHERE campaign_id = ? AND tenant_id = ? AND state != 'restored'
                """,
                (self.campaign_id, self.tenant_id),
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise RuntimeError(
                    f"workflow1 fixture checkpoint requires recovery: {active['checkpoint_id']}"
                )
            connection.execute(
                """
                INSERT INTO workflow1_fixture_checkpoints (
                    checkpoint_id, campaign_id, tenant_id, scenario,
                    original_json, original_digest, state
                ) VALUES (?, ?, ?, ?, ?, ?, 'prepared')
                """,
                (
                    checkpoint_id,
                    self.campaign_id,
                    self.tenant_id,
                    action.scenario,
                    original_json,
                    original.digest,
                ),
            )
            connection.commit()
        try:
            installed = self.corpus.install_conflict(original)
        except Exception:
            # The original checkpoint was durable before mutation; restore it now.
            self.corpus.replace(original)
            raise
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE workflow1_fixture_checkpoints
                SET installed_digest = ?, state = 'installed'
                WHERE checkpoint_id = ? AND state = 'prepared'
                """,
                (installed.digest, checkpoint_id),
            )
        event_id = recorder.store.append_event(
            "campaign.fixture.prepared",
            {
                "checkpoint_id": checkpoint_id,
                "scenario": action.scenario,
                "tenant_id": self.tenant_id,
                "original_digest": original.digest,
                "installed_digest": installed.digest,
            },
            correlation=CorrelationIds(operation_id=action.request.identity.operation_id),
        )
        return PreparedWorkflow1Scenario(
            checkpoint_id=checkpoint_id,
            evidence=(f"events.ndjson#{event_id}",),
        )

    def restore(
        self,
        action: WorkflowAction,
        prepared: PreparedWorkflow1Scenario,
        recorder: ActionRecorder,
    ) -> tuple[str, ...]:
        self._validate_action(action)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM workflow1_fixture_checkpoints
                WHERE checkpoint_id = ? AND campaign_id = ? AND tenant_id = ?
                """,
                (prepared.checkpoint_id, self.campaign_id, self.tenant_id),
            ).fetchone()
        if row is None or row["scenario"] != action.scenario:
            raise RuntimeError("workflow1 fixture checkpoint identity does not match")
        if row["state"] != "installed" or not row["installed_digest"]:
            raise RuntimeError("workflow1 fixture checkpoint is not restorable")
        if self.corpus.snapshot().digest != row["installed_digest"]:
            raise RuntimeError("workflow1 fixture corpus drifted after installation")
        original_rows = json.loads(row["original_json"])
        original = _snapshot(original_rows)
        if original.digest != row["original_digest"]:
            raise RuntimeError("workflow1 fixture checkpoint digest is corrupt")
        restored = self.corpus.replace(original)
        if restored.digest != row["original_digest"]:
            raise RuntimeError("workflow1 fixture exact restoration failed")
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE workflow1_fixture_checkpoints
                SET state = 'restored', restored_at = CURRENT_TIMESTAMP
                WHERE checkpoint_id = ? AND state = 'installed'
                """,
                (prepared.checkpoint_id,),
            ).rowcount
        if changed != 1:
            raise RuntimeError("workflow1 fixture restore transition raced")
        event_id = recorder.store.append_event(
            "campaign.fixture.restored",
            {
                "checkpoint_id": prepared.checkpoint_id,
                "scenario": action.scenario,
                "tenant_id": self.tenant_id,
                "restored_digest": restored.digest,
            },
            correlation=CorrelationIds(operation_id=action.request.identity.operation_id),
        )
        return (f"events.ndjson#{event_id}",)
