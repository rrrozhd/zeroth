"""Maturity upgrade must preserve unknown history and refuse lossy rollback."""

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.econ_plane.test_sdk_evidence_namespace import NOW, engine as database_engine
from zeroth.econ.plane.database import _missing_chain_owned_columns
from zeroth.econ.plane.instrumentation.models import OutcomeEvent
from zeroth.econ.plane.instrumentation.schemas import OutcomeQueryResponse

engine = database_engine


def test_maturity_migration_preserves_legacy_rows_and_refuses_lossy_rollback(engine):
    migration = importlib.import_module(
        "zeroth.econ.plane._migrations.versions.20260906_21_outcome_maturity"
    )
    with Session(engine) as raw:
        row = OutcomeEvent(
            tenant_id="tenant-a",
            join_key="run",
            execution_id="event",
            capability_id="workflow",
            implementation_id="v1",
            outcome_type="approval",
            outcome_payload_json={"value": True},
            outcome_value="True",
            occurred_at=NOW,
            outcome_timestamp=NOW,
            ingested_at=NOW,
            provenance="MEASURED",
        )
        raw.add(row)
        raw.commit()
        assert row.maturity is None
        assert OutcomeQueryResponse.model_validate(row).maturity == "unknown"
    with engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.downgrade()
        assert _missing_chain_owned_columns(conn) == (
            ("outcome_events", "maturity", "20260906_21"),
        )
        migration.upgrade()
        migration.upgrade()
        assert _missing_chain_owned_columns(conn) == ()
        assert conn.execute(
            text("SELECT outcome_value, provenance, maturity FROM outcome_events")
        ).one() == (
            "True",
            "MEASURED",
            None,
        )
        conn.execute(text("UPDATE outcome_events SET maturity='unknown'"))
        migration.downgrade()
        migration.upgrade()
        conn.execute(text("UPDATE outcome_events SET maturity='final'"))
        with pytest.raises(RuntimeError, match="outcome maturity"):
            migration.downgrade()
