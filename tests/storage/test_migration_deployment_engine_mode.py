"""Migration 015 pins every deployment to an immutable engine mode."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from tests.storage.test_migration_coordination import _config


def test_deployment_engine_mode_migration_backfills_legacy_rows(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'deployment-engine-mode.db'}"
    config = _config(database_url)
    command.upgrade(config, "014")
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO deployment_versions (
                        deployment_id, deployment_ref, version, graph_id,
                        graph_version, graph_version_ref, serialized_graph,
                        deployment_settings_snapshot, status, created_at, updated_at
                    ) VALUES (
                        'legacy-id', 'legacy-ref', 1, 'graph-1',
                        1, 'graph-1@1', '{}', '{}', 'active',
                        '2026-07-20T00:00:00+00:00', '2026-07-20T00:00:00+00:00'
                    )
                    """
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, "015")
    engine = sa.create_engine(database_url)
    try:
        columns = {column["name"]: column for column in sa.inspect(engine).get_columns(
            "deployment_versions"
        )}
        assert not columns["engine_mode"]["nullable"]
        assert not columns["attestation_payload_version"]["nullable"]
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT engine_mode, attestation_payload_version "
                    "FROM deployment_versions WHERE deployment_id = 'legacy-id'"
                )
            ).one()
        assert row.engine_mode == "legacy"
        assert row.attestation_payload_version == 1
    finally:
        engine.dispose()

    command.downgrade(config, "014")
    engine = sa.create_engine(database_url)
    try:
        names = {
            column["name"]
            for column in sa.inspect(engine).get_columns("deployment_versions")
        }
        assert "engine_mode" not in names
        assert "attestation_payload_version" not in names
    finally:
        engine.dispose()
