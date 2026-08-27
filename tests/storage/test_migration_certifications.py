"""Migration 027 certification schema invariants (SQLite, no Docker)."""

from __future__ import annotations

import sqlite3

import pytest

from zeroth.service.bootstrap.migrations import run_migrations


def test_027_is_greenfield_scoped_unique_and_events_are_append_only(tmp_path) -> None:
    database_path = tmp_path / "certifications.db"
    run_migrations(f"sqlite:///{database_path}")
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "029",
        )
        assert connection.execute("SELECT COUNT(*) FROM app_certifications").fetchone() == (0,)
        event_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(app_certification_events)")
        }
        assert {"promotion_target_key", "override_expires_at"} <= event_columns
        values = (
            "row-a",
            "tenant-a",
            "workspace-a",
            "cert-a",
            "{}",
            "sha256:" + "1" * 64,
            "certified",
            "2026-08-26T12:00:00+00:00",
            "2026-08-26T12:00:00+00:00",
        )
        connection.execute(
            "INSERT INTO app_certifications "
            "(row_id, tenant_id, workspace_id, certification_id, receipt_json, "
            "receipt_digest, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO app_certifications "
                "(row_id, tenant_id, workspace_id, certification_id, receipt_json, "
                "receipt_digest, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("row-b", *values[1:]),
            )
        connection.execute(
            "INSERT INTO app_certification_events "
            "(event_id, tenant_id, workspace_id, certification_id, event_type, state, "
            "promotion_target_key, actor_id, scopes_json, override_expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "event-a",
                "tenant-a",
                "workspace-a",
                "cert-a",
                "registered",
                "certified",
                "production/support-agent",
                "certifier",
                "[]",
                "2026-08-26T12:15:00+00:00",
                "2026-08-26T12:00:00+00:00",
            ),
        )
        assert connection.execute(
            "SELECT promotion_target_key, override_expires_at "
            "FROM app_certification_events WHERE event_id = 'event-a'"
        ).fetchone() == (
            "production/support-agent",
            "2026-08-26T12:15:00+00:00",
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE app_certification_events SET actor_id = 'other' "
                "WHERE event_id = 'event-a'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM app_certification_events WHERE event_id = 'event-a'")
    finally:
        connection.close()
