"""Persist the graph/deployment references that protect template versions."""

from __future__ import annotations

import json
from collections.abc import Iterable

from alembic import op
from sqlalchemy import text

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def _references(payload: str) -> Iterable[tuple[str, int | None, str]]:
    document = json.loads(payload)
    seen: set[tuple[str, int | None]] = set()
    for node in document.get("nodes", []):
        agent = node.get("agent") if isinstance(node, dict) else None
        reference = agent.get("template_ref") if isinstance(agent, dict) else None
        if not isinstance(reference, dict):
            continue
        name = reference.get("name")
        version = reference.get("version")
        if not isinstance(name, str) or not name:
            continue
        if version is not None and not isinstance(version, int):
            continue
        if (name, version) in seen:
            continue
        seen.add((name, version))
        yield name, version, "latest" if version is None else "explicit"


def _backfill() -> None:
    connection = op.get_bind()
    sources: list[tuple[str, str, str, str | None, str, str]] = []
    for row in connection.execute(
        text(
            "SELECT tenant_id, workspace_id, graph_id, version, payload "
            "FROM graph_versions WHERE status = 'published'"
        )
    ).mappings():
        sources.append(
            (
                "published_graph",
                f"{row['graph_id']}@{row['version']}",
                row["tenant_id"],
                row["workspace_id"],
                "null" if row["workspace_id"] is None else f"value:{row['workspace_id']}",
                row["payload"],
            )
        )
    for row in connection.execute(
        text(
            "SELECT tenant_id, workspace_id, deployment_ref, version, "
            "serialized_graph FROM deployment_versions"
        )
    ).mappings():
        sources.append(
            (
                "deployment",
                f"{row['deployment_ref']}@{row['version']}",
                row["tenant_id"],
                row["workspace_id"],
                "null" if row["workspace_id"] is None else f"value:{row['workspace_id']}",
                row["serialized_graph"],
            )
        )
    insert = text(
        "INSERT INTO template_dependency_references "
        "(tenant_id, workspace_id, workspace_scope, source_kind, source_ref, "
        "template_name, template_version, reference_mode, reference_key) "
        "VALUES (:tenant_id, :workspace_id, :workspace_scope, :source_kind, :source_ref, "
        ":template_name, :template_version, :reference_mode, :reference_key)"
    )
    for source_kind, source_ref, tenant_id, workspace_id, workspace_scope, payload in sources:
        for name, version, mode in _references(payload):
            connection.execute(
                insert,
                {
                    "tenant_id": tenant_id,
                    "workspace_id": workspace_id,
                    "workspace_scope": workspace_scope,
                    "source_kind": source_kind,
                    "source_ref": source_ref,
                    "template_name": name,
                    "template_version": version,
                    "reference_mode": mode,
                    "reference_key": "latest" if version is None else f"version:{version}",
                },
            )


def upgrade() -> None:
    op.execute("""
        CREATE TABLE template_dependency_references (
            tenant_id TEXT NOT NULL,
            workspace_id TEXT,
            workspace_scope TEXT NOT NULL,
            source_kind TEXT NOT NULL CHECK (source_kind IN ('published_graph', 'deployment')),
            source_ref TEXT NOT NULL,
            template_name TEXT NOT NULL,
            template_version INTEGER,
            reference_mode TEXT NOT NULL CHECK (reference_mode IN ('explicit', 'latest')),
            reference_key TEXT NOT NULL,
            CHECK (
                (workspace_id IS NULL AND workspace_scope = 'null') OR
                (workspace_id IS NOT NULL AND workspace_scope = 'value:' || workspace_id)
            ),
            CHECK (
                (reference_mode = 'latest' AND template_version IS NULL
                    AND reference_key = 'latest') OR
                (reference_mode = 'explicit' AND template_version >= 1)
            ),
            PRIMARY KEY (
                tenant_id, workspace_scope, source_kind, source_ref,
                template_name, reference_key
            )
        )
    """)
    op.execute(
        "CREATE INDEX idx_template_dependency_target "
        "ON template_dependency_references("
        "tenant_id, workspace_scope, template_name, reference_mode, template_version)"
    )
    _backfill()


def downgrade() -> None:
    op.execute("DROP TABLE template_dependency_references")
