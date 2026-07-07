from sqlalchemy import select
from sqlalchemy import text

from econ_plane.auth.models import Role
from econ_plane.database import Base, SessionLocal, engine


def bootstrap() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_schema_compat()
    with SessionLocal() as db:
        for role_name in ("Admin", "Analyst", "Approver", "Viewer"):
            existing = db.execute(select(Role).where(Role.name == role_name)).scalar_one_or_none()
            if existing is None:
                db.add(Role(name=role_name))
        db.commit()


def _ensure_schema_compat() -> None:
    with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect != "sqlite":
            return

        def has_column(table: str, column: str) -> bool:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            return any(r[1] == column for r in rows)

        def ensure_col(table: str, column: str, ddl: str) -> None:
            if has_column(table, "id") and not has_column(table, column):
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

        ensure_col("capabilities", "tenant_id", "tenant_id VARCHAR(128) DEFAULT 'tenant_default'")
        ensure_col("capabilities", "type", "type VARCHAR(64) DEFAULT 'RISK'")
        ensure_col("capabilities", "description", "description VARCHAR(1024) DEFAULT ''")
        ensure_col("capabilities", "criticality", "criticality VARCHAR(16) DEFAULT 'MED'")
        ensure_col("capabilities", "is_protected", "is_protected BOOLEAN DEFAULT 0")
        ensure_col("capabilities", "created_at", "created_at DATETIME")

        ensure_col("implementations", "tenant_id", "tenant_id VARCHAR(128) DEFAULT 'tenant_default'")
        ensure_col("implementations", "provider", "provider VARCHAR(32) DEFAULT 'custom'")
        ensure_col("implementations", "model_name", "model_name VARCHAR(255) DEFAULT ''")
        ensure_col("implementations", "model_version_hash", "model_version_hash VARCHAR(128) DEFAULT ''")
        ensure_col("implementations", "prompt_version_hash", "prompt_version_hash VARCHAR(128) DEFAULT ''")
        ensure_col("implementations", "pipeline_version_hash", "pipeline_version_hash VARCHAR(128) DEFAULT ''")
        ensure_col("implementations", "config_json", "config_json JSON DEFAULT '{}'")
        ensure_col("implementations", "status", "status VARCHAR(32) DEFAULT 'ACTIVE'")

        ensure_col("execution_events", "tenant_id", "tenant_id VARCHAR(128) DEFAULT 'tenant_default'")
        ensure_col("execution_events", "join_key", "join_key VARCHAR(128) DEFAULT ''")
        ensure_col("outcome_events", "tenant_id", "tenant_id VARCHAR(128) DEFAULT 'tenant_default'")
        ensure_col("outcome_events", "join_key", "join_key VARCHAR(128) DEFAULT ''")
        ensure_col("outcome_events", "implementation_id", "implementation_id VARCHAR(128)")
        ensure_col("outcome_events", "outcome_payload_json", "outcome_payload_json JSON DEFAULT '{}'")
        ensure_col("outcome_events", "occurred_at", "occurred_at DATETIME")
        ensure_col("outcome_events", "ingested_at", "ingested_at DATETIME")
        ensure_col("outcome_events", "provenance", "provenance VARCHAR(16) DEFAULT 'MEASURED'")

        ensure_col("value_estimates", "tenant_id", "tenant_id VARCHAR(128) DEFAULT 'tenant_default'")
        ensure_col("value_estimates", "implementation_id", "implementation_id VARCHAR(128)")
        ensure_col("value_estimates", "relative_interval_width", "relative_interval_width FLOAT DEFAULT 0.0")
        ensure_col("value_estimates", "confidence_gate_passed", "confidence_gate_passed BOOLEAN DEFAULT 0")
        ensure_col("value_estimates", "estimation_method_version", "estimation_method_version VARCHAR(32) DEFAULT 'v1'")
        ensure_col("value_estimates", "cost_data_quality", "cost_data_quality VARCHAR(32) DEFAULT 'measured'")
        ensure_col("value_estimates", "value_data_quality", "value_data_quality VARCHAR(32) DEFAULT 'measured'")
        ensure_col("value_estimates", "confidence_breakdown", "confidence_breakdown JSON DEFAULT '{}'")
        ensure_col("value_estimates", "interval_method", "interval_method VARCHAR(32) DEFAULT 'hierarchical'")

        ensure_col("valuation_runs", "tenant_id", "tenant_id VARCHAR(128) DEFAULT 'tenant_default'")
        ensure_col("valuation_runs", "implementation_id", "implementation_id VARCHAR(128)")

        ensure_col("performance_snapshots", "tenant_id", "tenant_id VARCHAR(128) DEFAULT 'tenant_default'")
        ensure_col("performance_snapshots", "implementation_id", "implementation_id VARCHAR(128)")
        ensure_col("performance_snapshots", "confidence_gate_passed", "confidence_gate_passed BOOLEAN DEFAULT 1")
        ensure_col("performance_snapshots", "confidence_breakdown", "confidence_breakdown JSON DEFAULT '{}'")
