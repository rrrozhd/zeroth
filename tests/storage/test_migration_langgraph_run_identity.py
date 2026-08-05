"""Migration 019 keys LangGraph attestations by signed run identity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from tests.storage.test_migration_coordination import _config


def _insert(
    database_url: str,
    *,
    correlation_id: str,
    payload: dict[str, object],
    signature: str,
) -> None:
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO langgraph_run_attestations "
                    "(tenant_id, deployment_ref, correlation_id, payload_json, signature, "
                    "signing_key_id, algorithm) "
                    "VALUES ('tenant-a', 'deployment-a', :correlation_id, :payload, "
                    ":signature, 'key-1', 'HMAC-SHA256')"
                ),
                {
                    "correlation_id": correlation_id,
                    "payload": json.dumps(payload, sort_keys=True),
                    "signature": signature,
                },
            )
    finally:
        engine.dispose()


def test_run_attestation_migration_backfills_and_roundtrips(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'langgraph-run-identity.db'}"
    config = _config(database_url)
    command.upgrade(config, "018")
    signed_payload = {"correlation_id": "corr-shared", "run_id": "run-signed"}
    legacy_payload = {"correlation_id": "corr-legacy"}
    _insert(
        database_url,
        correlation_id="corr-shared",
        payload=signed_payload,
        signature="signature-signed",
    )
    _insert(
        database_url,
        correlation_id="corr-legacy",
        payload=legacy_payload,
        signature="signature-legacy",
    )

    command.upgrade(config, "019")
    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        assert inspector.get_pk_constraint("langgraph_run_attestations")["constrained_columns"] == [
            "tenant_id",
            "deployment_ref",
            "run_id",
        ]
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.text(
                        "SELECT correlation_id, run_id, payload_json, signature "
                        "FROM langgraph_run_attestations ORDER BY correlation_id"
                    )
                )
                .mappings()
                .all()
            )
        assert [(row["correlation_id"], row["run_id"]) for row in rows] == [
            ("corr-legacy", "corr-legacy"),
            ("corr-shared", "run-signed"),
        ]
        assert [json.loads(row["payload_json"]) for row in rows] == [
            legacy_payload,
            signed_payload,
        ]
        assert [row["signature"] for row in rows] == [
            "signature-legacy",
            "signature-signed",
        ]
    finally:
        engine.dispose()

    command.downgrade(config, "018")
    engine = sa.create_engine(database_url)
    try:
        inspector = sa.inspect(engine)
        assert inspector.get_pk_constraint("langgraph_run_attestations")["constrained_columns"] == [
            "tenant_id",
            "deployment_ref",
            "correlation_id",
        ]
        assert "run_id" not in {
            column["name"] for column in inspector.get_columns("langgraph_run_attestations")
        }
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.text(
                        "SELECT correlation_id, payload_json, signature "
                        "FROM langgraph_run_attestations ORDER BY correlation_id"
                    )
                )
                .mappings()
                .all()
            )
        assert [json.loads(row["payload_json"]) for row in rows] == [
            legacy_payload,
            signed_payload,
        ]
        assert [row["signature"] for row in rows] == [
            "signature-legacy",
            "signature-signed",
        ]
    finally:
        engine.dispose()


def test_run_attestation_downgrade_refuses_correlation_collisions(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'langgraph-run-downgrade.db'}"
    config = _config(database_url)
    command.upgrade(config, "019")
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            for run_id in ("run-1", "run-2"):
                connection.execute(
                    sa.text(
                        "INSERT INTO langgraph_run_attestations "
                        "(tenant_id, deployment_ref, run_id, correlation_id, payload_json, "
                        "signature, signing_key_id, algorithm) VALUES "
                        "('tenant-a', 'deployment-a', :run_id, 'corr-shared', :payload, "
                        ":signature, 'key-1', 'HMAC-SHA256')"
                    ),
                    {
                        "run_id": run_id,
                        "payload": json.dumps(
                            {"correlation_id": "corr-shared", "run_id": run_id},
                            sort_keys=True,
                        ),
                        "signature": f"signature-{run_id}",
                    },
                )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="correlation collisions"):
        command.downgrade(config, "018")

    engine = sa.create_engine(database_url)
    try:
        with engine.connect() as connection:
            count = connection.execute(
                sa.text("SELECT COUNT(*) FROM langgraph_run_attestations")
            ).scalar_one()
        assert count == 2
    finally:
        engine.dispose()
