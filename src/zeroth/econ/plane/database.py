import logging
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Numeric, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from zeroth.econ.plane.config import settings
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.platform.storage.schema_revision import read_schema_revision
from zeroth.platform.storage.scoping import ScopeContext

logger = logging.getLogger(__name__)

_MIGRATIONS_PACKAGE = "zeroth.econ.plane._migrations"


class Base(DeclarativeBase):
    pass


class EconSchemaNotConverged(RuntimeError):
    """Raised at startup when a non-SQLite econ database is behind the chain.

    Distinct from a configuration error: the URL is right and the database is
    reachable: it is the schema in it that cannot serve the mappers.
    """


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


#: Columns the offline Alembic chain adds to tables the runtime maps, each with
#: the revision that adds it.  Deliberately *not* an ORM-versus-database diff:
#: most of what ``_ensure_sqlite_compat`` patches below (``capabilities.type``,
#: ``implementations.provider``, the ``value_estimates`` and
#: ``performance_snapshots`` columns) is added by no revision at all, so
#: reporting it under a "run alembic upgrade head" remedy would name a remedy
#: that does not work.  Every entry here is one ``alembic upgrade head`` fixes --
#: which ``test_econ_schema_convergence`` pins against a live database rather
#: than against this comment.
_CHAIN_OWNED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("users", "tenant_id", "20260811_04"),
    ("users", "workspace_id", "20260811_04"),
    ("connector_delivery_log", "tenant_id", "20260811_05"),
    ("execution_events", "cost_measurement", "20260812_04"),
    ("execution_events", "usage_measurement", "20260812_04"),
    ("execution_events", "campaign_id", "20260822_08"),
    ("execution_events", "operation_id", "20260822_08"),
    ("execution_events", "provider_request_id", "20260822_08"),
    ("execution_events", "cleanup_status", "20260822_08"),
    ("execution_events", "deployment_ref", "20260823_09"),
    ("execution_events", "evidence_kind", "20260823_09"),
    ("cost_reservations", "deployment_ref", "20260823_09"),
    ("cost_reservations", "evidence_kind", "20260823_09"),
    ("policy_actions", "enforcement_action_id", "20260812_06"),
)


def _missing_chain_owned_columns(conn: Connection) -> tuple[tuple[str, str, str], ...]:
    """Return the chain-owned columns a table in this database is missing.

    A table that is absent entirely is not drift: ``create_all`` runs before
    this and builds an absent table from the model, columns and all.  Drift is a
    table that already existed and that ``create_all``, being create-if-absent,
    skipped wholesale.
    """
    inspector = inspect(conn)
    present = set(inspector.get_table_names())
    columns: dict[str, set[str]] = {}
    missing = []
    for table, column, revision in _CHAIN_OWNED_COLUMNS:
        if table not in present:
            continue
        if table not in columns:
            columns[table] = {found["name"] for found in inspector.get_columns(table)}
        if column not in columns[table]:
            missing.append((table, column, revision))
    return tuple(missing)


def _require_converged_schema(conn: Connection) -> None:
    """Refuse to start on a non-SQLite database the Alembic chain has not reached.

    ``_migrations/`` is offline tooling that runtime never invokes
    (``PROVENANCE.md``), and ``create_all`` is create-if-absent and never alters
    an existing table -- so on PostgreSQL a column added by a revision reaches an
    existing database through ``alembic upgrade head`` and nothing else.  Until
    it does, a mapper names a column the table does not have and every read
    through it fails with ``UndefinedColumn``.

    Converging here instead -- issuing the ALTER from application code, the way
    the SQLite branch below does -- was the alternative, and it is the wrong one
    for a managed deployment: it takes DDL locks from every replica that starts,
    needs a DDL grant the application role often does not have, and leaves
    ``alembic_version`` untouched, so ``read_schema_revision`` would report
    "behind" on a schema that had been silently patched.  That evidence is on the
    health path; falsifying it to avoid an error message is a bad trade.  SQLite
    keeps converging: it ships no migration step at all, and one process owns
    the file.

    Failing here is loud by design.  ``econ/plane/main.py``'s startup event calls
    ``bootstrap()`` bare, and the bundled mount
    (``service/bootstrap/lifecycle.py``) catches only ``ImportError`` around it --
    it already raises ``RuntimeError`` from that same block for an econ
    misconfiguration -- so this stops the process rather than degrading the plane.
    """
    missing = _missing_chain_owned_columns(conn)
    if not missing:
        return
    revision = read_schema_revision(engine, _MIGRATIONS_PACKAGE)
    detail = ", ".join(
        f"{table}.{column} (revision {added_by})" for table, column, added_by in missing
    )
    raise EconSchemaNotConverged(
        f"econ database schema is behind the shipped migrations on {conn.dialect.name}: "
        f"{detail}. Applied revision {revision.applied!r}, shipped head "
        f"{revision.head!r} ({revision.state}). Run 'alembic upgrade head' against "
        "ECP_DATABASE_URL before starting the econ plane."
    )


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
            _require_converged_schema(conn)
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
        ensure_col("execution_events", "campaign_id", "campaign_id VARCHAR(128)")
        ensure_col("execution_events", "operation_id", "operation_id VARCHAR(192)")
        ensure_col(
            "execution_events", "provider_request_id", "provider_request_id VARCHAR(256)"
        )
        ensure_col("execution_events", "cleanup_status", "cleanup_status VARCHAR(64)")
        ensure_col("execution_events", "deployment_ref", "deployment_ref VARCHAR(192)")
        ensure_col(
            "execution_events",
            "evidence_kind",
            "evidence_kind VARCHAR(32) NOT NULL DEFAULT 'legacy_unknown'",
        )
        ensure_col("cost_reservations", "deployment_ref", "deployment_ref VARCHAR(192)")
        ensure_col(
            "cost_reservations",
            "evidence_kind",
            "evidence_kind VARCHAR(32) NOT NULL DEFAULT 'legacy_unknown'",
        )
        if conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cost_reservations'")
        ).first():
            conn.execute(
                text(
                    "UPDATE cost_reservations SET evidence_kind = 'synthetic_control' "
                    "WHERE operation_id LIKE 'control-gate:%'"
                )
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
        execution_types = {
            column["name"]: column["type"]
            for column in inspect(conn).get_columns("execution_events")
        }
        if any(
            (
                getattr(execution_types.get(column), "precision", None),
                getattr(execution_types.get(column), "scale", None),
            )
            != (18, 8)
            for column in cost_columns
            if column in execution_types
        ):
            _load_compat_migration(
                conn,
                "20260824_10_execution_cost_precision.py",
            ).upgrade()
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
        # through this call.  Executing revision 20260812_06 rather than restating
        # its DDL here is what stops the surfaces from drifting: a hand-written
        # ALTER did drift -- it carried the foreign key the revision omitted, and
        # neither carried the unique index the ORM now declares.  The revision owns
        # the column, the reference and the index; every surface gets what it says.
        link = _load_compat_migration(conn, "20260812_06_policy_action_link.py")
        try:
            link.upgrade()
        except link.DuplicatePolicyActionLink:
            # Same containment, and the same reasoning, as the identity refusal
            # below: the column is what the service reads, and it is already in
            # place by the time this raises; only the index is skipped.  Letting
            # the refusal escape engine.begin() would roll back every ALTER above
            # it.  The enforcement action is undecidable either way while the
            # duplicate stands -- the index would have prevented it, and cannot
            # repair it -- so this is reported, not converted into an outage.
            logger.warning(
                "econ policy-action link is not unique: more than one policy action "
                "is linked to a single enforcement action, so that action cannot be "
                "decided. Run revision 20260812_06 offline to see and reconcile them."
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
        except identity.DuplicateOutcomeIdentity:
            # The revision refuses rather than deleting a row out of an
            # erasure-audited table, which is right for an operator running it
            # offline.  Here it runs once per process start -- bootstrap() is
            # called from econ/plane/main.py's startup event and from
            # service/bootstrap/lifecycle.py for the bundled mount, and from
            # nowhere else; get_db/get_scoped_db are plain session factories
            # (pinned by tests/econ_plane/test_bootstrap_invariants.py).  So the
            # duplicate scan below is not a per-request cost.  It is still a full
            # scan of outcome_events at every start of a database that has not
            # converged, which is the price of knowing whether a unique index can
            # be built at all; it stops once one is.
            #
            # Propagating is what is refused, not because the cost is per
            # request but because of what is at stake: the index is a backstop
            # behind an identity check ingest already performs, so a database
            # without it is degraded, not broken -- unlike a missing *column*,
            # which no read survives and which _require_converged_schema
            # therefore does refuse to start on.  Contain it inside this
            # transaction -- letting it escape engine.begin() would roll back
            # every compatibility ALTER above -- and skip only the index.
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
