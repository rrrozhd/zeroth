"""Migration 016 adds the ZER-8 tool-enforcement tables."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import IntegrityError

from tests.storage.test_migration_coordination import _config

_TABLES = (
    "decision_records",
    "tool_inventory_registrations",
    "run_attestations",
    "enforcement_heartbeats",
)


def test_enforcement_tables_migration_creates_all_four_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'enforcement-tables.db'}"
    config = _config(database_url)
    command.upgrade(config, "015")
    engine = sa.create_engine(database_url)
    try:
        tables = set(sa.inspect(engine).get_table_names())
        assert not set(_TABLES) & tables
    finally:
        engine.dispose()

    command.upgrade(config, "016")
    engine = sa.create_engine(database_url)
    try:
        tables = set(sa.inspect(engine).get_table_names())
        assert set(_TABLES) <= tables
    finally:
        engine.dispose()


def test_decision_records_idempotency_key_unique_per_tenant(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'decision-records-idempotency.db'}"
    command.upgrade(_config(database_url), "016")
    engine = sa.create_engine(database_url)
    try:
        insert = sa.text(
            """
            INSERT INTO decision_records (
                decision_id, tenant_id, idempotency_key, request_digest,
                decision_kind, reason_code, policy_version, deployment_ref,
                principal_id, action_name, action_fingerprint, created_at
            ) VALUES (
                :decision_id, :tenant_id, :idempotency_key, :request_digest,
                'allow', 'ok', 'policy-1', 'deployment-1',
                'principal-1', 'tool.call', 'fingerprint-1',
                '2026-07-30T00:00:00+00:00'
            )
            """
        )
        with engine.begin() as connection:
            connection.execute(
                insert,
                {
                    "decision_id": "decision-1",
                    "tenant_id": "tenant-a",
                    "idempotency_key": "idem-1",
                    "request_digest": "digest-1",
                },
            )
        # Same key, same tenant -> rejected as a duplicate.
        with engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(
                insert,
                {
                    "decision_id": "decision-2",
                    "tenant_id": "tenant-a",
                    "idempotency_key": "idem-1",
                    "request_digest": "digest-1",
                },
            )
        # Same key, different tenant -> accepted as a distinct record.
        with engine.begin() as connection:
            connection.execute(
                insert,
                {
                    "decision_id": "decision-3",
                    "tenant_id": "tenant-b",
                    "idempotency_key": "idem-1",
                    "request_digest": "digest-1",
                },
            )
        with engine.connect() as connection:
            count = connection.execute(
                sa.text("SELECT COUNT(*) FROM decision_records")
            ).scalar_one()
        assert count == 2
    finally:
        engine.dispose()


def test_run_attestations_correlation_id_unique_per_tenant(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'run-attestations-correlation.db'}"
    command.upgrade(_config(database_url), "016")
    engine = sa.create_engine(database_url)
    try:
        insert = sa.text(
            """
            INSERT INTO run_attestations (
                attestation_id, tenant_id, correlation_id, deployment_ref,
                graph_version, adapter_version, inventory_fingerprint,
                inventory_coverage, tool_count, claimed_level, payload_json,
                digest, issued_at, expires_at, created_at
            ) VALUES (
                :attestation_id, :tenant_id, :correlation_id, 'deployment-1',
                'graph-1', 'adapter-1', 'fingerprint-1',
                'complete', 3, 'enforced', '{}',
                'digest-1', '2026-07-30T00:00:00+00:00',
                '2026-07-30T01:00:00+00:00', '2026-07-30T00:00:00+00:00'
            )
            """
        )
        with engine.begin() as connection:
            connection.execute(
                insert,
                {
                    "attestation_id": "attestation-1",
                    "tenant_id": "tenant-a",
                    "correlation_id": "correlation-1",
                },
            )
        # Same correlation id, same tenant -> rejected as a duplicate.
        with engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(
                insert,
                {
                    "attestation_id": "attestation-2",
                    "tenant_id": "tenant-a",
                    "correlation_id": "correlation-1",
                },
            )
        # Same correlation id, different tenant -> accepted as a distinct record.
        with engine.begin() as connection:
            connection.execute(
                insert,
                {
                    "attestation_id": "attestation-3",
                    "tenant_id": "tenant-b",
                    "correlation_id": "correlation-1",
                },
            )
        with engine.connect() as connection:
            count = connection.execute(
                sa.text("SELECT COUNT(*) FROM run_attestations")
            ).scalar_one()
        assert count == 2
    finally:
        engine.dispose()


def test_enforcement_tables_migration_downgrade_drops_all_four_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'enforcement-tables-downgrade.db'}"
    config = _config(database_url)
    command.upgrade(config, "016")
    command.downgrade(config, "015")
    engine = sa.create_engine(database_url)
    try:
        tables = set(sa.inspect(engine).get_table_names())
        assert not set(_TABLES) & tables
    finally:
        engine.dispose()
