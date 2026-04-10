"""Database table definitions and migrations for storing graphs.

This module holds the SQL that creates and updates the ``graph_versions``
table.  The repository uses these migrations automatically when it starts up.
"""

from __future__ import annotations

from zeroth.core.storage import Migration

GRAPH_SCHEMA_VERSION = 2
GRAPH_SCHEMA_SCOPE = "graphs"

GRAPH_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="create_graph_versions_table",
        sql="""
        CREATE TABLE IF NOT EXISTS graph_versions (
            graph_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(graph_id, version)
        );

        CREATE INDEX IF NOT EXISTS idx_graph_versions_graph_id_version
        ON graph_versions(graph_id, version DESC);

        CREATE INDEX IF NOT EXISTS idx_graph_versions_status
        ON graph_versions(status);
        """,
    ),
    Migration(
        version=2,
        name="drop_legacy_graphs_table",
        sql="""
        DROP TABLE IF EXISTS graphs;
        """,
    ),
)
