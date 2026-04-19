"""SQLite helpers and migration support for Zeroth repositories.

Provides a thin wrapper around Python's sqlite3 module that adds
automatic schema migrations, transaction management, and sensible
default settings (WAL mode, foreign keys).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet


@dataclass(frozen=True, slots=True)
class Migration:
    """A single database schema change, identified by a version number.

    Migrations run in order (version 1, then 2, etc.) and each one
    contains a SQL script that creates or alters tables.
    """

    version: int
    name: str
    sql: str


class EncryptedField:
    """Symmetric field encryption helper for sensitive JSON columns."""

    def __init__(self, key: str | bytes) -> None:
        self._fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)

    @classmethod
    def generate_key(cls) -> str:
        return Fernet.generate_key().decode("utf-8")

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("utf-8")).decode("utf-8")


class SQLiteDatabase:
    """A lightweight SQLite wrapper that manages connections and schema versions.

    Handles opening connections with good defaults, running transactions,
    and applying migrations so your database schema stays up to date.
    """

    def __init__(self, path: str | Path, *, encryption_key: str | bytes | None = None):
        self.path = str(path)
        self.encrypted_field = (
            EncryptedField(encryption_key) if encryption_key is not None else None
        )

    def connect(self) -> sqlite3.Connection:
        """Open a new database connection with recommended settings.

        Enables foreign keys, WAL journaling mode, and row-factory access
        so you can use column names to read results.
        """
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        self._ensure_schema_table(connection)
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open a connection, yield it, then commit or rollback automatically.

        Use this as a context manager (with-statement). If your code raises
        an exception, the transaction is rolled back; otherwise it's committed.
        """
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fetch_schema_version(self, scope: str) -> int:
        """Return the current schema version number for a given scope.

        Returns 0 if no migrations have been applied yet.
        """
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT version FROM schema_versions WHERE scope = ?",
                (scope,),
            ).fetchone()
        return int(row["version"]) if row else 0

    def apply_migrations(self, scope: str, migrations: Sequence[Migration]) -> list[Migration]:
        """Run any pending migrations for a scope and return the ones that were applied.

        Migrations are applied in version order. Already-applied migrations
        are skipped. Raises ValueError if migrations have duplicate or
        non-contiguous version numbers.
        """
        ordered = self._validate_migrations(migrations)
        applied: list[Migration] = []
        with self.transaction() as connection:
            current_version = self._fetch_schema_version(connection, scope)
            for migration in ordered:
                if migration.version <= current_version:
                    continue
                connection.executescript(migration.sql)
                connection.execute(
                    """
                    INSERT INTO schema_versions(scope, version, name)
                    VALUES(?, ?, ?)
                    ON CONFLICT(scope) DO UPDATE SET
                        version = excluded.version,
                        name = excluded.name,
                        applied_at = CURRENT_TIMESTAMP
                    """,
                    (scope, migration.version, migration.name),
                )
                current_version = migration.version
                applied.append(migration)
        return applied

    def execute_script(self, sql: str) -> None:
        """Run a raw SQL script inside a transaction."""
        with self.transaction() as connection:
            connection.executescript(sql)

    def _ensure_schema_table(self, connection: sqlite3.Connection) -> None:
        """Create the schema_versions tracking table if it doesn't exist yet."""
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_versions (
                scope TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    def _fetch_schema_version(self, connection: sqlite3.Connection, scope: str) -> int:
        """Read the current schema version from an existing connection."""
        row = connection.execute(
            "SELECT version FROM schema_versions WHERE scope = ?",
            (scope,),
        ).fetchone()
        return int(row["version"]) if row else 0

    def _validate_migrations(self, migrations: Sequence[Migration]) -> list[Migration]:
        """Sort migrations and check that versions are contiguous starting at 1."""
        ordered = sorted(migrations, key=lambda item: item.version)
        seen: set[int] = set()
        expected_version = 1
        for migration in ordered:
            if migration.version in seen:
                msg = f"duplicate migration version {migration.version}"
                raise ValueError(msg)
            if migration.version != expected_version:
                msg = (
                    "migration versions must be contiguous starting at 1; "
                    f"expected {expected_version}, got {migration.version}"
                )
                raise ValueError(msg)
            seen.add(migration.version)
            expected_version += 1
        return ordered
