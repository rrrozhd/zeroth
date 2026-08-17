"""Certifier-owned database inspection around one candidate migration process."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import AppDeclaration, file_digest

MigrationProcess = Callable[[str, str], None]


def _postgres_tables(database_url: str) -> list[str]:
    import psycopg

    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_schema || '.' || table_name "
            "FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "AND table_type = 'BASE TABLE' ORDER BY table_schema, table_name"
        )
        return [row[0] for row in cursor.fetchall()]


def _postgres_migration_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    raise ValueError("postgres migration DSN must use a PostgreSQL URL")


def _sqlite_tables(database: Path) -> list[str]:
    if not database.is_file() or database.stat().st_size == 0:
        raise ValueError("app migration did not create a non-empty SQLite database")
    with sqlite3.connect(database) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]


def inspect_migration(
    declaration: AppDeclaration,
    run_candidate: MigrationProcess,
    *,
    backend: str,
    postgres_dsn: str | None = None,
) -> dict[str, Any]:
    """Measure schema state in this process around only the candidate migration."""
    reference = declaration.targets.migration_runner
    if backend == "postgres":
        if not postgres_dsn:
            raise ValueError("postgres migration certification requires a DSN")
        database_url = postgres_dsn
        if _postgres_tables(database_url):
            raise ValueError("postgres migration certification requires a fresh database")
        run_candidate(reference, _postgres_migration_url(database_url))
        objects = _postgres_tables(database_url)
        if not objects:
            raise ValueError("app migration did not create PostgreSQL tables")
        digest = (
            "sha256:"
            + hashlib.sha256(json.dumps(objects, separators=(",", ":")).encode()).hexdigest()
        )
    elif backend == "sqlite":
        with tempfile.TemporaryDirectory(prefix="zeroth-app-migration-") as directory:
            database = Path(directory) / "migration.sqlite"
            run_candidate(reference, f"sqlite:///{database}")
            objects = _sqlite_tables(database)
            if not objects:
                raise ValueError("app migration did not create SQLite tables")
            digest = file_digest(database)
    else:
        raise ValueError(f"declared database backend {backend!r} is unsupported")
    return {
        "backend": backend,
        "object_count": len(objects),
        "runner": reference,
        "schema_sha256": digest,
    }
