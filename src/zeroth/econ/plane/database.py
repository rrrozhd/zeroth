import logging
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Numeric, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from zeroth.econ.plane.config import settings
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.scoping import ScopeContext

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_scoped_db(context: ScopeContext) -> Generator[ScopedSession, None, None]:
    """Yield a tenant-scoped econ gateway from an explicit trusted context."""
    db = SessionLocal()
    try:
        yield ScopedSession(db, context)
    finally:
        db.close()


def _load_compat_migration(conn: Connection, filename: str) -> ModuleType:
    """Load an econ revision as a fresh module whose ``op`` is bound to ``conn``.

    Executing the revision itself -- rather than restating its DDL here -- is
    what keeps the runtime convergence path and the offline Alembic chain from
    drifting: the index names, columns and expressions are the migration's own.
    A fresh module per call mirrors Alembic's own migration test harness and
    avoids sharing the module-level ``Operations`` proxy across startups.
    """
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration_path = Path(__file__).parent / "_migrations/versions" / filename
    spec = importlib.util.spec_from_file_location(
        f"_zeroth_runtime_{migration_path.stem}",
        migration_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load econ compatibility migration: {filename}")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.op = Operations(MigrationContext.configure(conn))
    return migration


def _ensure_sqlite_compat() -> None:
    with engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return

        def has_column(table: str, column: str) -> bool:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            return any(r[1] == column for r in rows)

        def ensure_col(table: str, column: str, ddl: str) -> None:
            if has_column(table, column):
                return
            table_exists = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :table"),
                {"table": table},
            ).first()
            if table_exists:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

        tenant_ownership_tables = (
            "users",
            "deployment_implementations",
            "cost_profiles",
            "cost_estimates",
            "ground_truth_costs",
            "calibration_metrics",
            "dashboard_views",
            "enforcement_actions",
            "traffic_policies",
            "budget_policies",
            "audit_log",
        )
        for table in tenant_ownership_tables:
            ensure_col(table, "tenant_id", "tenant_id VARCHAR(128) DEFAULT 'tenant_default'")
        ensure_col("connector_delivery_log", "tenant_id", "tenant_id VARCHAR(128)")

        ensure_col("users", "workspace_id", "workspace_id VARCHAR(128)")
        if conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'")
        ).first():
            conn.execute(
                text(
                    "UPDATE users SET tenant_id = 'default' "
                    "WHERE tenant_id IS NULL OR tenant_id = 'tenant_default'"
                )
            )

        ensure_col("capabilities", "tenant_id", "tenant_id VARCHAR(128) DEFAULT 'tenant_default'")
        ensure_col("capabilities", "type", "type VARCHAR(64) DEFAULT 'RISK'")
        ensure_col("capabilities", "description", "description VARCHAR(1024) DEFAULT ''")
        ensure_col("capabilities", "criticality", "criticality VARCHAR(16) DEFAULT 'MED'")
        ensure_col("capabilities", "is_protected", "is_protected BOOLEAN DEFAULT 0")
        ensure_col("capabilities", "created_at", "created_at DATETIME")

        ensure_col("implementations", "tenant_id", "tenant_id VARCHAR(128) DEFAULT 'tenant_default'")  # noqa: E501
        ensure_col("implementations", "provider", "provider VARCHAR(32) DEFAULT 'custom'")
        ensure_col("implementations", "model_name", "model_name VARCHAR(255) DEFAULT ''")
        ensure_col("implementations", "model_version_hash", "model_version_hash VARCHAR(128) DEFAULT ''")  # noqa: E501
        ensure_col("implementations", "prompt_version_hash", "prompt_version_hash VARCHAR(128) DEFAULT ''")  # noqa: E501
        ensure_col("implementations", "pipeline_version_hash", "pipeline_version_hash VARCHAR(128) DEFAULT ''")  # noqa: E501
        ensure_col("implementations", "config_json", "config_json JSON DEFAULT '{}'")
        ensure_col("implementations", "status", "status VARCHAR(32) DEFAULT 'ACTIVE'")

        ensure_col("execution_events", "tenant_id", "tenant_id VARCHAR(128)")
        ensure_col("execution_events", "join_key", "join_key VARCHAR(128) DEFAULT ''")
        ensure_col(
            "execution_events",
            "cost_measurement",
            "cost_measurement VARCHAR(16) DEFAULT 'unmeasured'",
        )
        ensure_col(
            "execution_events",
            "usage_measurement",
            "usage_measurement VARCHAR(16) DEFAULT 'unmeasured'",
        )
        execution_columns = {
            row[1]: row for row in conn.execute(text("PRAGMA table_info(execution_events)"))
        }
        cost_columns = ("token_cost_usd", "tool_cost_usd", "compute_cost_usd")
        if any(execution_columns.get(column, (None,) * 4)[3] for column in cost_columns):
            operations = Operations(MigrationContext.configure(conn))
            with operations.batch_alter_table(
                "execution_events", recreate="always"
            ) as batch:
                for column in cost_columns:
                    batch.alter_column(
                        column,
                        existing_type=Numeric(12, 4),
                        nullable=True,
                    )
        ensure_col("outcome_events", "tenant_id", "tenant_id VARCHAR(128) DEFAULT 'tenant_default'")
        ensure_col("outcome_events", "join_key", "join_key VARCHAR(128) DEFAULT ''")
        ensure_col("outcome_events", "implementation_id", "implementation_id VARCHAR(128)")
        ensure_col("outcome_events", "outcome_payload_json", "outcome_payload_json JSON DEFAULT '{}'")  # noqa: E501
        ensure_col("outcome_events", "occurred_at", "occurred_at DATETIME")
        ensure_col("outcome_events", "ingested_at", "ingested_at DATETIME")
        ensure_col("outcome_events", "provenance", "provenance VARCHAR(16) DEFAULT 'MEASURED'")

        ensure_col("value_estimates", "relative_interval_width", "relative_interval_width FLOAT DEFAULT 0.0")  # noqa: E501
        ensure_col("value_estimates", "confidence_gate_passed", "confidence_gate_passed BOOLEAN DEFAULT 0")  # noqa: E501
        ensure_col("value_estimates", "estimation_method_version", "estimation_method_version VARCHAR(32) DEFAULT 'v1'")  # noqa: E501
        ensure_col("value_estimates", "cost_data_quality", "cost_data_quality VARCHAR(32) DEFAULT 'measured'")  # noqa: E501
        ensure_col("value_estimates", "value_data_quality", "value_data_quality VARCHAR(32) DEFAULT 'measured'")  # noqa: E501
        ensure_col("value_estimates", "confidence_breakdown", "confidence_breakdown JSON DEFAULT '{}'")  # noqa: E501
        ensure_col("value_estimates", "interval_method", "interval_method VARCHAR(32) DEFAULT 'hierarchical'")  # noqa: E501
        ensure_col("value_estimates", "tenant_id", "tenant_id VARCHAR(128)")
        ensure_col("value_estimates", "implementation_id", "implementation_id VARCHAR(128)")

        ensure_col("valuation_runs", "tenant_id", "tenant_id VARCHAR(128)")
        ensure_col("valuation_runs", "implementation_id", "implementation_id VARCHAR(128)")

        ensure_col("performance_snapshots", "confidence_gate_passed", "confidence_gate_passed BOOLEAN DEFAULT 1")  # noqa: E501
        ensure_col("performance_snapshots", "tenant_id", "tenant_id VARCHAR(128) DEFAULT 'tenant_default'")  # noqa: E501
        ensure_col("performance_snapshots", "implementation_id", "implementation_id VARCHAR(128)")
        ensure_col("performance_snapshots", "confidence_breakdown", "confidence_breakdown JSON DEFAULT '{}'")  # noqa: E501

        # Base.metadata.create_all is create-if-absent and never alters an existing
        # table, so a pre-existing database only reaches the policy/enforcement link
        # column through this ALTER.  Nullable with no default and no backfill: NULL
        # means "unlinked", which the enforcement service refuses to resolve by
        # recency (A01-11).
        ensure_col(
            "policy_actions",
            "enforcement_action_id",
            "enforcement_action_id INTEGER REFERENCES enforcement_actions(id)",
        )

        # Converge pre-Alembic/create_all compatibility databases on the same
        # ownership constraints as revision 20260811_05.
        _load_compat_migration(conn, "20260811_05_tenant_scope.py").upgrade()

        # ...and on the outcome identity of revision 20260812_07.  A fresh
        # database gets that index from OutcomeEvent.__table_args__, but
        # create_all(checkfirst=True) skips a *pre-existing* table wholesale and
        # never alters one, while _migrations/ is offline Postgres tooling that
        # runtime never invokes (PROVENANCE.md).  This call is the only door the
        # index reaches such a database through -- and it has to run after
        # 20260811_05, whose SQLite batch ALTER rebuilds the table from a
        # reflection that cannot see an expression index.  The revision returns
        # early once the index exists, so the duplicate scan it performs is
        # bounded to databases that have not converged yet.
        identity = _load_compat_migration(conn, "20260812_07_outcome_event_identity.py")
        try:
            identity.upgrade()
        except RuntimeError:
            # The revision refuses rather than deleting a row out of an
            # erasure-audited table, which is right for an operator running it
            # offline.  Here it runs per request and at startup, so propagating
            # would turn a data condition into a total outage of the plane.
            # Contain it inside this transaction -- letting it escape
            # engine.begin() would roll back every compatibility ALTER above --
            # and skip only the index; ingest still pre-checks the identity.
            #
            # The refusal names the colliding rows by join_key, which is the
            # erasure subject key (SqlAlchemyEconEventEraser deletes by it).
            # Interpolating it here would republish, into application logs and
            # on every subsequent request, precisely what an erasure receipt
            # exists to retire.  Point at the offline revision instead.
            logger.warning(
                "econ outcome identity index not converged: outcome_events holds rows "
                "that collide on it. Run revision 20260812_07 offline to see and "
                "reconcile them; outcome ingest pre-checks the identity meanwhile."
            )
